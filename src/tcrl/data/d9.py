"""Build D9 paired conversion data, training replay and frozen diagnostic selectors."""
import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
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
OPERATIONS = (
    ("percent", "ratio", "number", "Decimal fraction on the 0 to 1 scale."),
    ("distance", "metres", "integer", "Distance in meters."),
    ("top_k", "count", "integer", "Number of ranked items to return, highest score first."),
    ("boolean", "include_notes", "boolean", "Whether to include annotation notes. Default false."),
    ("duration", "seconds", "integer", "Duration in seconds."),
    ("relative_time", "offset_seconds", "integer", "Signed offset from the reference time, in seconds."),
    ("encoding", "encoded", "string", "Standard padded Base64 of the UTF-8 label."),
)


def synthetic():
    import base64

    for domain, item, split in DOMAINS:
        rng = random.Random(f"{SEED}:{domain}")
        for kind, field, typ, description in OPERATIONS:
            for i in range(14):
                n = rng.sample(range(12, 99), 3)
                code = f"{item}-{n[0]}-{i}"
                if kind == "percent":
                    value, source = n[1] / 100, f"{n[1]}%"
                elif kind == "distance":
                    value, source = n[1] * 1000, f"{n[1]} kilometers"
                elif kind == "duration":
                    value, source = n[1] * 60, f"{n[1]} minutes"
                elif kind == "relative_time":
                    value, source = -n[1] * 3600, f"{n[1]} hours before the reference time"
                elif kind == "top_k":
                    value, source = (1, "the single highest-ranked entry") if i % 2 == 0 else (3, "the three highest-ranked entries")
                elif kind == "boolean":
                    value, source = True, "show the annotations"
                else:
                    value = base64.b64encode(code.encode()).decode()
                    source = f"the label {code!r} in Base64"
                key, ref = f"{domain}_{field}", f"{domain}_item"
                params = {key: {"type": typ, "description": description}}
                if kind == "boolean":
                    params[key]["default"] = False
                def tool(name, desc, props, required):
                    return {"name": f"{domain}_{name}", "description": desc,
                            "parameters": {"type": "object", "properties": props, "required": required}}
                tools = [
                    tool(f"set_{kind}", f"Apply {description} to the {domain} record.",
                         params, [] if kind == "boolean" else [key]),
                    tool("lookup", f"Retrieve an item record from the {domain}.",
                         {ref: {"type": "string"}}, [ref]),
                ]
                literal = json.dumps(value)
                rendered = {
                    "train": f"Look up the {item} {code}. For the {domain} {kind.replace('_', ' ')} setting, use {{value}}.",
                    "gate": f"For {code}, retrieve its {domain} record and set the {kind.replace('_', ' ')} option to {{value}}.",
                    "heldout": f"Two actions for {code} at the {domain}: fetch its record and apply {{value}} as the {kind.replace('_', ' ')} setting.",
                }[split]
                yield {"id": f"d9:{domain}:{kind}:{i:02}", "subset": "d9-synthetic", "kind": "call",
                       "source": "synthetic", "partition": split, "conversion": kind, "refusal": None,
                       "question": rendered.format(value=source), "literal_question": rendered.format(value=literal),
                       "tools": tools, "gold": [{"name": tools[1]["name"], "arguments": {ref: code}},
                                                 {"name": tools[0]["name"], "arguments": {key: value}}],
                       "plan": f"Look up {code}. Express {source} as {literal} for the {kind.replace('_', ' ')} setting."}


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
