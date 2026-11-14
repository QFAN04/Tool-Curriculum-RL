"""Build D9 paired conversion data, training replay and frozen diagnostic selectors."""
import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
from itertools import combinations
from pathlib import Path
import random
import re

import pyarrow as pa
import pyarrow.parquet as pq

from tcrl.curriculum.freeze import read, write
from tcrl.data.build import convert, grounded
from tcrl.data.export import rl_rows, sft_rows
from tcrl.data.split import gold_families, visible_families

SEED = 20261118
PERCENT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:%|percent\b)", re.I)
UNITS = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(km|cm|minutes?|hours?)\b", re.I)
UNIT_RULES = {"km": (1000, "m", r"metres?|meters?"), "cm": (Decimal("0.01"), "m", r"metres?|meters?"),
              "minute": (60, "s", r"seconds?"), "hour": (3600, "s", r"seconds?")}


def raw_evidence(key, value, spec, question):
    description = spec.get("description", "").lower()
    if type(value) is bool and ("default" in spec and type(spec["default"]) is bool
                               and value != spec["default"] and "_" in key):
        label = key.replace("_", " ")
        word = "include" if value else "exclude"
        match = re.search(rf"\b{word}\s+(?:the\s+)?{re.escape(label.removeprefix('include '))}\b", question, re.I)
        if match:
            return "boolean", match.span(), f"set {label} to {str(value).lower()}"
    if type(value) not in (int, float):
        return None
    target = Decimal(str(value))
    if re.search(r"fraction|decimal|0\s*(?:to|[-–])\s*1\b", description):
        matches = [m for m in PERCENT.finditer(question) if Decimal(m[1]) / 100 == target]
        if len(matches) == 1:
            return "percent", matches[0].span(), f"{value} as a decimal fraction"
    for match in UNITS.finditer(question):
        factor, unit, wording = UNIT_RULES[match[2].lower().rstrip("s")]
        if re.search(wording, description) and Decimal(match[1]) * factor == target:
            return "unit", match.span(), f"{value} {unit}"
    if value == 1 and key in {"top_n", "top_k", "limit"}:
        match = re.search(r"\b(the (?:single )?(?:highest|lowest|most recent))\b", question, re.I)
        if match:
            return "top_k", match.span(), f"the top {value} result, ranked by the requested criterion,"
    return None


def paired_raw(record):
    tools = {tool["name"]: tool for tool in record["tools"]}
    missing = []
    for call in record["gold"]:
        params = tools[call["name"]].get("parameters") or {}
        props, required = params.get("properties", {}), params.get("required", [])
        for key, value in call["arguments"].items():
            if not grounded({key: value}, record["question"], props, required):
                missing.append(raw_evidence(key, value, props[key], record["question"]))
    if len(missing) != 1 or missing[0] is None:
        return None
    category, (start, stop), literal = missing[0]
    return {**record, "conversion": category, "source": "toucan", "literal_question":
            record["question"][:start] + literal + record["question"][stop:]}


def native_plan(row):
    messages = json.loads(row["messages"])
    start = next(i for i, m in enumerate(messages) if m["role"] == "tool_call")
    return "\n".join(m["content"] for m in messages[:start] if m["role"] == "assistant").strip()


def scan(root, static_calls):
    stats = Counter()
    found = []
    warmup = {}
    for path in sorted(root.glob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=512):
            for row in batch.to_pylist():
                stats["raw_rows"] += 1
                original = Counter()
                converted = convert(row, original)
                existing = static_calls.get(row["uuid"])
                if existing and converted and all(existing[k] == converted[k] for k in ("question", "tools", "gold")):
                    plan = native_plan(row)
                    if plan and len(plan) <= 450 and "<" not in plan:
                        warmup[row["uuid"]] = {**existing, "plan": plan}
                if converted is not None or not original["drop_ungrounded"]:
                    continue
                record = convert(row, Counter(), require_grounded=False)
                if not record:
                    continue
                stats["grounded_only"] += 1
                plan = native_plan(row)
                if plan and "<" not in plan:
                    stats["native_plan"] += 1
                pair = paired_raw(record)
                if pair is not None:
                    stats["verified_candidate"] += 1
                    found.append({**pair, "plan": plan})
    return found, stats, warmup


# Fictional tools and values, independent of BFCL and of the Toucan gold labels.
DOMAINS = (
    ("gallery", "frame", "train"), ("library", "shelf", "train"),
    ("theater", "seat", "train"), ("garden", "plot", "train"),
    ("museum", "exhibit", "train"), ("bakery", "display", "train"),
    ("school", "classroom", "gate"), ("station", "platform", "gate"),
    ("studio", "booth", "heldout"), ("aquarium", "tank", "heldout"),
    ("observatory", "dome", "heldout"), ("stadium", "section", "heldout"),
)
OPERATIONS = {
    "percent": ("ratio", "number", "Decimal fraction on the 0 to 1 scale."),
    "distance": ("metres", "integer", "Distance in meters."),
    "top_k": ("count", "integer", "Number of ranked items to return, highest score first."),
    "boolean": ("include_notes", "boolean", "Whether to include annotation notes."),
    "duration": ("seconds", "integer", "Duration in seconds."),
    "relative_time": ("offset_seconds", "integer", "Signed offset from the reference time, in seconds."),
    "rate_total": ("total_cents", "integer", "Total cost in cents, including any stated discount."),
}
NUMBERS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth")
BOOLEAN = {
    True: ("show the annotations", "keep the notes visible", "leave the annotations turned on",
           "include the commentary", "make the notes available", "display the notes alongside the record"),
    False: ("hide the annotations", "leave out the notes", "turn off commentary",
            "show the record without notes", "suppress the annotations", "keep the notes private"),
}
TEMPLATES = {
    "train": (
        "Find the {item} record {code}, and update these settings: {settings}.",
        "For {code}, pull up its {item} record. Here are the settings I need: {settings}.",
        "Please apply {settings} for {code}. Also retrieve its {item} record.",
        "I'm working on {code}. Get its {item} record and change {settings}.",
    ),
    "gate": (
        "I need three actions for {code}: retrieve the {item} record and configure {settings}.",
        "Could you handle {settings} for {code}? I also need the current {item} record.",
    ),
    "heldout": (
        "At the {domain}, will you fetch the {item} record for {code} and apply {settings}?",
        "My request for {code} has a lookup and two updates. Fetch its {item} record, then set {settings}.",
        "Update {settings} on {code}; separately, return the matching {item} record.",
    ),
}


def conversion_value(kind, index, rng):
    n = rng.randint(2, 25)
    style = index % 3
    if kind == "percent":
        return n / 100, (f"{n}%", f"{n} percent", f"{n} out of every 100")[style]
    if kind == "distance":
        return n * 1000, (f"{n} km", f"{n} kilometers", f"{n} kilometres")[style]
    if kind == "duration":
        return n * 60, (f"{n} minutes", f"{n} min", f"a window of {n} minutes")[style]
    if kind == "relative_time":
        sign = 1 if index % 2 else -1
        direction = ("after", "ahead of", "following") if sign == 1 else ("before", "prior to", "earlier than")
        return sign * n * 3600, f"{n} hours {direction[style]} the reference time"
    if kind == "top_k":
        i = index % 10
        phrases = (f"the top {NUMBERS[i]} by score", f"ranked entries through the {ORDINALS[i]} place",
                   f"a shortlist of {i + 1}, highest scores first")
        return i + 1, phrases[(index // 10) % len(phrases)]
    if kind == "boolean":
        value = bool(index % 2)
        return value, BOOLEAN[value][(index // 2) % len(BOOLEAN[value])]
    price, quantity = rng.randint(2, 9), rng.randint(2, 12)
    if style == 2:
        return price * quantity * 90, f"{quantity} units at ${price} each, less a 10% discount"
    return price * quantity * 100, (f"{quantity} items at ${price} apiece", f"${price} per item for {quantity} items")[style]


def domain_tools(domain):
    def tool(name, description, key, schema):
        return {"name": f"{domain}_{name}", "description": description,
                "parameters": {"type": "object", "properties": {key: schema}, "required": [key]}}
    tools = {"lookup": tool("lookup", f"Retrieve an item record from the {domain}.",
                            f"{domain}_item", {"type": "string"})}
    for kind, (field, typ, description) in OPERATIONS.items():
        tools[kind] = tool(f"set_{kind}", f"Apply {description} to the {domain} record.", f"{domain}_{field}",
                           {"type": typ, "description": description})
    return tools


def synthetic():
    operation_pairs = list(combinations(OPERATIONS, 2))
    for domain, item, split in DOMAINS:
        rng = random.Random(f"{SEED}:{domain}")
        pairs = operation_pairs * 4 + rng.sample(operation_pairs, 14)
        rng.shuffle(pairs)
        tools = domain_tools(domain)
        visits = Counter()
        for i, pair in enumerate(pairs):
            if rng.randrange(2):
                pair = pair[::-1]
            code = f"{item}-{rng.randint(12, 99)}-{i}"
            specs = []
            for kind in pair:
                value, expression = conversion_value(kind, visits[kind], rng)
                visits[kind] += 1
                specs.append((kind, value, expression))
            def call(kind, value):
                tool = tools[kind]
                key, = tool["parameters"]["properties"]
                return {"name": tool["name"], "arguments": {key: value}}
            def settings(literal):
                return " and ".join(f"{kind.replace('_', ' ')} to {json.dumps(value) if literal else expression}"
                                    for kind, value, expression in specs)
            template = rng.choice(TEMPLATES[split])
            visible = list(tools.values())
            rng.shuffle(visible)
            yield {"id": f"d9:{domain}:{'+'.join(pair)}:{i:02}", "subset": "d9-synthetic", "kind": "call",
                   "source": "synthetic", "partition": split, "conversion": "+".join(sorted(pair)), "refusal": None,
                   "question": template.format(item=item, code=code, domain=domain, settings=settings(False)),
                   "literal_question": template.format(item=item, code=code, domain=domain, settings=settings(True)),
                   "tools": visible, "gold": [call("lookup", code), *(call(kind, value) for kind, value, _ in specs)],
                   "plan": f"Retrieve {code}. " + " ".join(
                       f"For {kind.replace('_', ' ')}, {expression} gives {json.dumps(value)}."
                       for kind, value, expression in specs)}



def export(records, path, split):
    pq.write_table(pa.Table.from_pylist(rl_rows(records, split)), path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("raw", "static", "dev", "bfcl-scores", "out"):
        p.add_argument(f"--{name}", type=Path, required=True)
    args = p.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    static_all = read(args.static)
    synthetic_rows = list(synthetic())
    raw, stats, native = scan(args.raw, {r["id"]: r for r in static_all if r["kind"] == "call"})
    write(out / "toucan_candidates_review_only.jsonl", raw)
    sets = {partition: [row for row in synthetic_rows if row["partition"] == partition]
            for partition in ("train", "gate", "heldout")}
    static = [r for r in static_all if r["kind"] == "refuse"]
    static.sort(key=lambda r: r["id"])
    refusals = random.Random(SEED).sample(static, round(len(sets["train"]) * .15 / .85))
    train = sets["train"] + refusals
    sets["train"] = train
    families = {k: set().union(*(visible_families(r) for r in rows)) for k, rows in sets.items()}
    assert not (families["train"] & families["gate"] or families["train"] & families["heldout"]
                or families["gate"] & families["heldout"])
    export(train, out / "train.parquet", "d9_train")
    write(out / "train_sft.jsonl", sft_rows(train, reverse_frac=0))
    write(out / "train_records.jsonl", train)
    def with_plan(row, plan):
        messages = row["messages"]
        return {**row, "messages": [*messages[:-1], {**messages[-1],
                 "content": f"<think>\n{plan}\n</think>\n{messages[-1]['content']}"}]}
    plans = {r["id"]: r.get("plan") or "The available tools do not support this request." for r in train}
    write(out / "train_thinking_sft.jsonl", [with_plan(r, plans[r["id"]]) for r in sft_rows(train, reverse_frac=0)])
    # Format warmup uses only training-side rows and a deterministic balanced subset.
    warm = random.Random(SEED).sample([native[k] for k in sorted(native)], 102) + refusals[:18]
    warm_plans = {r["id"]: r.get("plan") or plans[r["id"]] for r in warm}
    assert not set().union(*(visible_families(r) for r in warm)) & (families["gate"] | families["heldout"])
    write(out / "warmup_thinking_sft.jsonl", [with_plan(r, warm_plans[r["id"]])
                                               for r in sft_rows(warm, reverse_frac=0)])
    stats["warmup_eligible_native"] = len(native)
    stats["warmup_selected_native"] = 102
    for split in ("gate", "heldout"):
        rows = sets[split]
        write(out / f"{split}_pairs.jsonl", rows)
        variants = [{**r, "id": f"{r['id']}:{variant}",
                     "question": r["literal_question"] if variant == "literal" else r["question"]}
                    for r in rows for variant in ("literal", "conversion")]
        export(variants, out / f"{split}.parquet", f"d9_{split}")
    dev = pq.ParquetFile(args.dev).read()
    indexes = [i for i, row in enumerate(dev["extra_info"].to_pylist())
               if row["split"] == "dev_unseen" and row["n_calls"] >= 2]
    unseen_indexes = [i for i, row in enumerate(dev["extra_info"].to_pylist()) if row["split"] == "dev_unseen"]
    assert len(indexes) == 70 and len(unseen_indexes) == 541
    pq.write_table(dev.take(pa.array(unseen_indexes)), out / "external_dev541.parquet")
    pq.write_table(dev.take(pa.array(sorted(random.Random(SEED).sample(indexes, 20)))), out / "think_probe20.parquet")
    pq.write_table(dev.take(pa.array(indexes)), out / "think_dev70.parquet")
    def hits(arm):
        return {r["id"] for r in json.loads((args.bfcl_scores / f"{arm}.score.json").read_text())["samples"]
                if r["correct"]}
    misses = hits("b1") - set().union(*(hits(a) for a in ("e0", "e0plus", "e1", "e2a", "e2b", "e2b_gold_only")))
    calls = sorted(r for r in misses if not r.startswith("irrelevance_"))
    assert len(calls) == 78 and len(misses) == 112
    (out / "bfcl_miss78.ids").write_text("\n".join(calls) + "\n", encoding="utf8")
    (out / "bfcl_thinking112.ids").write_text("\n".join(sorted(misses)) + "\n", encoding="utf8")
    report = {"raw": dict(stats), "raw_categories": dict(Counter(r["conversion"] for r in raw)),
              "synthetic_pair_counts": {split: dict(sorted(Counter(r["conversion"] for r in rows if r["kind"] == "call").items()))
                                        for split, rows in sets.items()},
              "raw_gold_families": len(set().union(*(gold_families(r) for r in raw))),
              "partitions": {k: {"rows_or_pairs": len(v), "refusals": sum(r["kind"] == "refuse" for r in v),
                                  "visible_families": sorted(families[k]),
                                  "gold_family_count": len(set().union(*(gold_families(r) for r in v)))}
                             for k, v in sets.items()},
              "sha256": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(out.iterdir())
                         if f.is_file() and f.name != "manifest.json"}}
    (out / "manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("sha256", "partitions")}))


if __name__ == "__main__":
    main()
