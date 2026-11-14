"""Classify archived E0 rollouts for failure-driven curriculum selection."""
import argparse
import json
from collections import Counter
from pathlib import Path

from tcrl.data.split import gold_families
from tcrl.reward import calls_match
from tcrl.tool_env import canonical, parse_tool_calls, tool_index


def strip_optional_defaults(calls, tools):
    schemas = tool_index(tools)
    normalized = []
    for call in calls:
        schema = schemas.get(call["name"], {}).get("parameters") or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or ())
        args = dict(call["arguments"])
        for key, value in call["arguments"].items():
            spec = props.get(key, {})
            if key in required or "default" not in spec:
                continue
            def single(val):
                return canonical({"name": call["name"], "arguments": {key: val}})
            if single(value) == single(spec["default"]):
                del args[key]
        normalized.append({**call, "arguments": args})
    return normalized


def classify(record, output):
    parsed = parse_tool_calls(output)
    if not parsed.ok:
        return "format"
    gold = record["gold"]
    if calls_match(parsed.calls, gold):
        return "strict_correct"
    if calls_match(strip_optional_defaults(parsed.calls, record["tools"]),
                   strip_optional_defaults(gold, record["tools"])):
        return "default_only"
    if len(parsed.calls) != len(gold):
        return "call_count"
    if Counter(call["name"] for call in parsed.calls) != Counter(call["name"] for call in gold):
        return "tool"
    return "arguments"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("sft", "rl", "samples", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()

    def read(path):
        with path.open(encoding="utf-8") as handle:
            return list(map(json.loads, handle))

    train = read(args.sft)
    rl = {row["id"]: row for row in read(args.rl)}
    freq = Counter(family for row in [*train, *rl.values()] for family in gold_families(row))
    counts = Counter()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for sample in read(args.samples):
            row = rl.pop(sample["id"])
            if not row["gold"]:
                continue
            outcomes = Counter(classify(row, output) for output in sample["outputs"])
            counts.update(outcomes)
            if not any(outcomes[k] for k in ("call_count", "tool", "arguments")):
                continue
            record = {
                **row, "outcomes": outcomes, "multi_call": len(row["gold"]) >= 2,
                "low_frequency_families": sorted(f for f in gold_families(row) if freq[f] < 10),
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            counts["eligible_seeds"] += 1
    if rl:
        raise ValueError("Expected archived E0 outputs for every RL training row")
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
