"""Summarize paired eight-rollout probes on identical seed exposure strata."""
import argparse
from collections import Counter
import json
from pathlib import Path

import pyarrow.parquet as pq

from tcrl.tool_env import canonical


def read(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf8").splitlines()]


def summarize(samples, target, seed_ids, gold_overlap=None):
    counts = Counter()
    seen = set()
    for row in samples:
        key, scores = row["id"], row["scores"]
        if key in seen or len(scores) != 8 or any(score not in (0, 1) for score in scores):
            raise ValueError(f"Invalid probe row: {key}")
        seen.add(key)
        passed = sum(scores)
        counts[key] = "all_right" if passed == 8 else "mixed" if passed else "all_wrong"
    if seen != target:
        raise ValueError("Probe IDs do not match the frozen targeted pool")
    strata = {"all": target, "non_seed": target - set.union(*seed_ids.values())}
    strata.update({f"{arm}_seed": target & ids for arm, ids in seed_ids.items()})
    strata["any_seed"] = target - strata["non_seed"]
    if gold_overlap is not None:
        strata["non_seed_no_gold_overlap"] = strata["non_seed"] - gold_overlap
    result = {}
    for name, ids in strata.items():
        group = Counter(counts[key] for key in ids)
        result[name] = {
            "n": len(ids), **{key: group[key] for key in ("all_wrong", "mixed", "all_right")},
            "mixed_fraction": group["mixed"] / len(ids) if ids else None,
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("target", "cu-seeds", "cf-seeds", "cu", "cf", "out"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    rows = pq.read_table(args.target, columns=["extra_info", "reward_model"]).to_pylist()
    target = {row["extra_info"]["id"] for row in rows}
    if len(target) != len(rows):
        raise ValueError("Target parquet contains duplicate IDs")
    manifests = {arm: read(getattr(args, f"{arm}_seeds")) for arm in ("cu", "cf")}
    seeds = {arm: {row["id"] for row in items} for arm, items in manifests.items()}
    if any(len(ids) != 200 for ids in seeds.values()):
        raise ValueError("Expected both complete 200-item seed manifests")
    def gold_key(calls):
        return tuple(sorted(canonical(call) for call in calls))

    seed_gold = {gold_key(row["gold"]) for items in manifests.values() for row in items}
    overlap = {row["extra_info"]["id"] for row in rows
               if gold_key(json.loads(row["reward_model"]["ground_truth"])) in seed_gold}
    result = {arm: summarize(read(getattr(args, arm)), target, seeds, overlap) for arm in ("cu", "cf")}
    result["non_seed_gate_pass"] = all(
        (result[arm]["non_seed"]["mixed_fraction"] or 0) >= 0.20 for arm in ("cu", "cf")
    )
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    main()
