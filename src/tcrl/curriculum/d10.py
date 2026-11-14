"""D10 seed selection, structured self-generation and fixed-budget curriculum."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import random
import tarfile
from time import monotonic

from tcrl.curriculum.freeze import read, write
from tcrl.data.d9 import BOOLEAN, DOMAINS, NUMBERS, OPERATIONS, ORDINALS, domain_tools, export
from tcrl.data.export import sft_rows
from tcrl.data.split import visible_families
from tcrl.eval.d9_gate import APPROVED_ARCHIVE_SHA256
from tcrl.prompts import CHAT_KWARGS, render_tools
from tcrl.reward import calls_match
from tcrl.tool_env import validate_call


SEED = 20261119
ATTEMPTS = 1200
TRAIN_CALLS = 360
REPLAY = 64
ARMS = ("cu", "cf")
WEAK = {"rate_total", "relative_time", "duration"}
D9_MANIFEST = "c47feddf4e9592835012f86ea0582ebdec2666135ee05a7589489a4a09c486d6"
TRAIN_DOMAINS = {domain: item for domain, item, split in DOMAINS if split == "train"}
FRAMES = (
    "Retrieve the {item} record {code}, then adjust {updates}.",
    "For {code}, please configure {updates}, and fetch its matching {item} record.",
    "Look up {code} in the {domain} and set {updates} for its {item} record.",
    "I need the {item} record for {code}. Please also apply {updates}.",
)
RANGES = {
    "percent": ((2, 25), (0, 2)), "distance": ((2, 25), (0, 2)),
    "duration": ((2, 25), (0, 2)), "relative_time": ((2, 25), (0, 5)),
    "top_k": ((1, 10), (0, 2)), "boolean": ((0, 1), (0, 5)),
    "rate_total": ((2, 9), (2, 12), (0, 2)),
}


def checked_inputs(data, archive):
    raw = (data / "manifest.json").read_bytes()
    if sha256(raw).hexdigest() != D9_MANIFEST:
        raise ValueError("D9 manifest changed")
    manifest = json.loads(raw)
    for name in ("train_records.jsonl", "gate.parquet", "gate_pairs.jsonl", "heldout.parquet",
                 "heldout_pairs.jsonl", "external_dev541.parquet"):
        if sha256((data / name).read_bytes()).hexdigest() != manifest["sha256"][name]:
            raise ValueError(f"Frozen D9 file changed: {name}")
    if sha256(archive.read_bytes()).hexdigest() != APPROVED_ARCHIVE_SHA256:
        raise ValueError("Archived E0 n=8 trajectories changed")
    with tarfile.open(archive, "r:gz") as handle:
        samples = [json.loads(line) for line in handle.extractfile("gate/train_probe/samples.jsonl")]
    return manifest, samples


def seed_pool(records, samples):
    scores = {}
    for sample in samples:
        values = sample["scores"]
        if sample["id"] in scores or len(values) != 8 or any(x not in (0, 1) for x in values):
            raise ValueError("Expected eight strict scores per unique E0 seed")
        scores[sample["id"]] = sample
    if set(scores) != {row["id"] for row in records} or len(scores) != 692:
        raise ValueError("Expected the complete 692-row D9 training probe")
    mastered, failures, literal, replay = [], [], [], []
    for row in sorted(records, key=lambda r: r["id"]):
        sample = scores[row["id"]]
        if sample["kind"] != row["kind"] or not calls_match(json.loads(sample["gold"]), row["gold"]):
            raise ValueError(f"Probe gold mismatch: {row['id']}")
        if row["kind"] == "refuse":
            replay.append(row)
            continue
        domain = row["id"].split(":")[1]
        if domain not in TRAIN_DOMAINS or row["partition"] != "train":
            raise ValueError("Seed outside D9 training domains")
        hits = int(sum(sample["scores"]))
        scored = {**row, "domain": domain, "pass_count": hits, "mode": "conversion"}
        if hits == 8:
            mastered.append(scored)
        else:
            failures.append(scored)
            literal.append({**scored, "id": f"{row['id']}:literal", "mode": "literal",
                            "question": row["literal_question"], "pass_count": None})
    if (len(mastered), len(failures), len(replay)) != (138, 450, 104):
        raise ValueError("Unexpected E0 probe counts")
    rng = random.Random(SEED)
    common = mastered + rng.sample(literal, 366)
    weak = rng.sample(failures, 196)
    static = rng.sample(common, TRAIN_CALLS)
    return common + weak, static, rng.sample(replay, REPLAY)


def schema_for(kinds):
    def integer(low, high):
        return {"type": "integer", "minimum": low, "maximum": high}

    props = {"frame": integer(0, len(FRAMES) - 1)}
    for label, kind in zip(("x", "y"), kinds, strict=True):
        props.update({f"{label}{i}": integer(*bounds) for i, bounds in enumerate(RANGES[kind])})
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def suggested_schema(seed, index):
    schema = schema_for(seed["conversion"].split("+"))
    # Vary operand suggestions so E0 sees alternatives to the same round numbers.
    rng = random.Random(f"{SEED}:operands:{index}")
    for key, spec in schema["properties"].items():
        if key == "frame":
            continue
        low, high = spec["minimum"], spec["maximum"]
        if high - low >= 5:
            start = rng.randint(low, high - 2)
            spec.update(minimum=start, maximum=start + 2)
    return schema


def solve(kind, args):
    n, style, *tail = args
    if kind == "rate_total":
        quantity, variant = style, tail[0]
        expressions = (f"{quantity} units at ${n} each", f"${n} per item for {quantity} items",
                       f"{quantity} units at ${n} each with a 10% discount")
        return n * quantity * (90 if variant == 2 else 100), expressions[variant]
    if kind == "relative_time":
        direction = ("before", "earlier than", "prior to", "after", "following", "ahead of")[style]
        return (-1 if style < 3 else 1) * n * 3600, f"{n} hours {direction} the reference time"
    if kind == "top_k":
        return n, (f"the top {NUMBERS[n - 1]} by score", f"entries through {ORDINALS[n - 1]} place",
                   f"a shortlist of {n}, highest scores first")[style]
    if kind == "boolean":
        return bool(n), BOOLEAN[bool(n)][style]
    scale, phrases = {
        "percent": (100, (f"{n}%", f"{n} percent", f"{n} in every 100")),
        "duration": (60, (f"{n} minutes", f"{n} min", f"a window of {n} minutes")),
        "distance": (1000, (f"{n} km", f"{n} kilometers", f"{n} kilometres")),
    }[kind]
    return (n / scale if kind == "percent" else n * scale), phrases[style]


def materialize(seed, raw, index, arm):
    kinds = seed["conversion"].split("+")
    domain = seed["domain"]
    item = TRAIN_DOMAINS[domain]
    schema = schema_for(kinds)["properties"]
    program = json.loads(raw)
    if not isinstance(program, dict) or program.keys() != schema.keys() or any(
        type(v) is not int or not schema[k]["minimum"] <= v <= schema[k]["maximum"] for k, v in program.items()
    ):
        raise ValueError("Generated task specification violates its operand schema")
    tools = domain_tools(domain)
    code = f"{item}-d10-{index:04}"
    mode = seed["mode"]

    def call(kind, value):
        tool = tools[kind]
        key, = tool["parameters"]["properties"]
        return {"name": tool["name"], "arguments": {key: value}}

    gold, settings = [call("lookup", code)], []
    for label, kind in zip(("x", "y"), kinds, strict=True):
        args = [program[f"{label}{i}"] for i in range(len(RANGES[kind]))]
        value, expression = solve(kind, args)
        gold.append(call(kind, value))
        wording = json.dumps(value) if mode == "literal" else expression
        settings.append(f"{kind.replace('_', ' ')} to {wording}")
    visible = list(tools.values())
    random.Random(f"{SEED}:{domain}:{index}").shuffle(visible)
    if any(validate_call(c, visible) for c in gold):
        raise ValueError("Solved calls violate the tool schema")
    question = FRAMES[program["frame"]].format(item=item, code=code, domain=domain, updates=" and ".join(settings))
    return {"id": f"d10:{arm}:{index:04}", "kind": "call", "tools": visible, "question": question, "gold": gold,
            "seed_id": seed["id"], "seed_pass_count": seed["pass_count"], "domain": domain,
            "conversion": seed["conversion"], "mode": mode, "program": program}


def generation_messages(seed, index):
    kinds = seed["conversion"].split("+")
    properties = suggested_schema(seed, index)["properties"]
    return [
        {"role": "system", "content": "Create a new three-call training example as a JSON task specification. "
         "The renderer turns your operands into a user request and validates the answer. Use only these tools.\n"
         + render_tools(seed["tools"])},
        {"role": "user", "content": f"Seed request: {seed['question']}\nExample number: {index}. "
         f"Create a fresh {seed['mode']} example with lookup and these two operations in order: {kinds}. "
         f"Prefer fresh integers in these suggested field ranges: {json.dumps(properties)}. "
         "Use x0, x1 (and x2 if present) for the first operation and y0, y1 (and y2 if present) for the second. "
         "Use frame 0 through 3 to choose a sentence pattern. Return only the task specification."},
    ]


def draw_seeds(pool, arm):
    rng = random.Random(f"{SEED}:{arm}")
    if arm == "cu":
        return rng.choices(pool, k=ATTEMPTS)
    weak = [r for r in pool if r["pass_count"] is not None and r["pass_count"] < 8]
    return rng.choices(weak, weights=[8 - r["pass_count"] for r in weak], k=ATTEMPTS)


def signature(row):
    # Exclude the new lookup ID and sentence frame from the novelty check.
    return row["domain"], row["mode"], json.dumps(sorted(row["gold"][1:], key=lambda c: c["name"]), sort_keys=True)


def generate(model, pool, originals, out, manifest):
    from transformers import AutoTokenizer
    from vllm import LLM, SamplingParams
    from vllm.sampling_params import StructuredOutputsParams

    tok = AutoTokenizer.from_pretrained(model)
    llm = LLM(model=model, max_model_len=8192, gpu_memory_utilization=.85, seed=SEED,
              enable_prefix_caching=True)
    allowed = set(manifest["partitions"]["train"]["visible_families"])
    forbidden = set().union(*(manifest["partitions"][name]["visible_families"] for name in ("gate", "heldout")))
    original_keys = set()
    for row in originals:
        if row["kind"] == "call":
            for mode in ("literal", "conversion"):
                original_keys.add(signature({**row, "domain": row["id"].split(":")[1], "mode": mode}))
    reports = {}
    constraints = {row["conversion"]: StructuredOutputsParams(json=schema_for(row["conversion"].split("+")))
                   for row in pool}
    for arm in ARMS:
        started = monotonic()
        seeds = draw_seeds(pool, arm)
        prompts = [tok.apply_chat_template(generation_messages(seed, i), tokenize=False,
                                           add_generation_prompt=True, **CHAT_KWARGS)
                   for i, seed in enumerate(seeds)]
        params = [SamplingParams(temperature=.9, top_p=1, max_tokens=128, seed=SEED + i,
                                 structured_outputs=constraints[seed["conversion"]])
                  for i, seed in enumerate(seeds)]
        outputs = llm.generate(prompts, params)
        candidates, audit = [], []
        seen = set()
        for i, (seed, result) in enumerate(zip(seeds, outputs, strict=True)):
            text = result.outputs[0].text
            entry = {"attempt": i, "seed_id": seed["id"], "raw": text}
            try:
                if result.outputs[0].finish_reason == "length":
                    raise ValueError("truncated")
                row = materialize(seed, text, i, arm)
            except ValueError as error:
                audit.append({**entry, "status": "invalid", "reason": str(error)})
                continue
            families = visible_families(row)
            if not families <= allowed or families & forbidden:
                raise ValueError(f"Out-of-split tool family: {row['id']}")
            key = signature(row)
            if key in original_keys or key in seen:
                audit.append({**entry, "status": "original_duplicate" if key in original_keys else "duplicate"})
                continue
            seen.add(key)
            candidates.append(row)
            audit.append({**entry, "status": "valid", "id": row["id"]})
        if not candidates:
            write(out / f"{arm}_attempts.jsonl", audit)
            raise ValueError(f"{arm} generated no valid questions")
        write(out / f"{arm}_attempts.jsonl", audit)
        write(out / f"{arm}_candidates.jsonl", candidates)
        reports[arm] = {"statuses": Counter(r["status"] for r in audit),
                        "seed_modes": Counter(r["mode"] for r in seeds),
                        "failure_seed_attempts": sum(r["pass_count"] is not None and r["pass_count"] < 8 for r in seeds),
                        "elapsed_seconds": round(monotonic() - started, 2)}
    (out / "generation.json").write_text(json.dumps(reports, indent=2) + "\n", encoding="utf8")
    combined = [row for arm in ARMS for row in read(out / f"{arm}_candidates.jsonl")]
    export(combined, out / "generated.parquet", "d10_generated")


def freeze(out, probe):
    samples = read(probe)
    counts = {}
    for row in samples:
        scores = row["scores"]
        if row["id"] in counts or len(scores) != 8 or any(s not in (0, 1) for s in scores):
            raise ValueError("Expected unique eight-way strict probe results")
        counts[row["id"]] = int(sum(scores))
    candidates = {arm: read(out / f"{arm}_candidates.jsonl") for arm in ARMS}
    if counts.keys() != {row["id"] for rows in candidates.values() for row in rows}:
        raise ValueError("Probe and generated candidate IDs disagree")
    replay = read(out / "replay.jsonl")
    report = {}
    chosen = {"cs": read(out / "static.jsonl")}
    for arm, rows in candidates.items():
        mixed = [row for row in rows if 0 < counts[row["id"]] < 8]
        full = [row for row in rows if counts[row["id"]] == 8]
        rng = random.Random(f"{SEED}:{arm}:freeze")
        report[arm] = {"generated": len(rows), "pass_count": Counter(counts[row["id"]] for row in rows),
                       "weak_conversion": sum(row["mode"] == "conversion" and
                                              bool(WEAK.intersection(row["conversion"].split("+"))) for row in rows),
                       "mixed_fraction": len(mixed) / len(rows), "mixed_per_attempt": len(mixed) / ATTEMPTS,
                       "mode": Counter(row["mode"] for row in rows)}
        report[arm]["weak_conversion_fraction"] = report[arm]["weak_conversion"] / len(rows)
        report[arm]["weak_conversion_per_attempt"] = report[arm]["weak_conversion"] / ATTEMPTS
        if len(mixed) + len(full) >= TRAIN_CALLS:
            chosen[arm] = (rng.sample(mixed, TRAIN_CALLS) if len(mixed) >= TRAIN_CALLS else
                           mixed + rng.sample(full, TRAIN_CALLS - len(mixed)))
            report[arm]["selected_mixed"] = sum(counts[r["id"]] < 8 for r in chosen[arm])
        report[arm]["shortfall"] = max(0, TRAIN_CALLS - len(mixed) - len(full))
    report["cross_arm_common_signatures"] = len(set(map(signature, candidates["cu"])) &
                                                set(map(signature, candidates["cf"])))
    (out / "screen.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    if len(chosen) != 3:
        raise ValueError("The fixed generation budget did not yield 360 eligible calls in both arms")
    for arm, rows in chosen.items():
        if len(rows) != TRAIN_CALLS or len(replay) != REPLAY:
            raise ValueError("Unequal SFT budgets")
        write(out / f"{arm}_sft.jsonl", sft_rows(rows + replay, reverse_frac=0))
        write(out / f"{arm}_selected.jsonl", rows)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("mode", choices=("prepare", "generate", "freeze"))
    cli.add_argument("--data", type=Path, required=True)
    cli.add_argument("--archive", type=Path, required=True)
    cli.add_argument("--out", type=Path, required=True)
    cli.add_argument("--model")
    cli.add_argument("--probe", type=Path)
    args = cli.parse_args()
    if args.mode == "freeze":
        freeze(args.out, args.probe)
        return
    manifest, samples = checked_inputs(args.data, args.archive)
    if args.mode == "generate":
        generate(args.model, read(args.out / "pool.jsonl"), read(args.data / "train_records.jsonl"), args.out, manifest)
        return
    pool, static, replay = seed_pool(read(args.data / "train_records.jsonl"), samples)
    args.out.mkdir(parents=True, exist_ok=True)
    allowed = set(manifest["partitions"]["train"]["visible_families"])
    forbidden = set().union(*(manifest["partitions"][name]["visible_families"] for name in ("gate", "heldout")))
    for row in pool + replay:
        families = visible_families(row)
        if not families <= allowed or families & forbidden:
            raise ValueError(f"Out-of-split seed tools: {row['id']}")
    for name, rows in (("pool", pool), ("static", static), ("replay", replay)):
        write(args.out / f"{name}.jsonl", rows)
    print(json.dumps({"pool": Counter(str(row["pass_count"]) for row in pool),
                      "static_mode": Counter(row["mode"] for row in static), "replay": len(replay)}))


if __name__ == "__main__":
    main()
