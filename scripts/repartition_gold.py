"""Repartition the existing training pool without sharing call gold keys."""
import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tcrl.data.export import rl_rows, sft_rows
from tcrl.data.split import SEED, gold_families, gold_key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("data/processed/splits"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    names = ("sft", "rl")
    original = {
        name: [json.loads(line) for line in (args.source / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()]
        for name in names
    }
    groups = defaultdict(list)
    result = {name: [] for name in names}
    targets = {}
    for name, rows in original.items():
        targets[name] = sum(row["kind"] == "call" for row in rows)
        for row in rows:
            if row["kind"] == "call":
                groups[gold_key(row)].append((name, row))
            else:
                result[name].append(row)

    rng = random.Random(SEED)
    ordered = sorted(groups)
    rng.shuffle(ordered)
    for key in sorted(ordered, key=lambda key: -len(groups[key])):
        group = groups[key]
        votes = Counter(name for name, _ in group)
        feasible = [name for name in names if targets[name] >= len(group)]
        if not feasible:
            raise ValueError("cannot fit a gold-key group into the remaining quotas")
        destination = max(feasible, key=lambda name: (votes[name], targets[name]))
        result[destination].extend(row for _, row in group)
        targets[destination] -= len(group)

    old_ids = Counter(row["id"] for rows in original.values() for row in rows)
    new_ids = Counter(row["id"] for rows in result.values() for row in rows)
    keys = [{gold_key(row) for row in result[name] if row["kind"] == "call"} for name in names]
    assert old_ids == new_ids and all(count == 1 for count in new_ids.values())
    assert not (keys[0] & keys[1]) and not any(targets.values())

    splits_dir = args.out / "splits"
    export_dir = args.out / "export"
    splits_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)

    def write_jsonl(path, rows):
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    for name, rows in result.items():
        rng.shuffle(rows)
        write_jsonl(splits_dir / f"{name}.jsonl", rows)
        calls = [row for row in rows if row["kind"] == "call"]
        families = {family for row in calls for family in gold_families(row)}
        old_families = {family for row in original[name] if row["kind"] == "call" for family in gold_families(row)}
        print(f"{name}: {len(calls)} calls, {len(rows) - len(calls)} refusals, "
              f"{len(families)} gold families (previously {len(old_families)})")

    write_jsonl(export_dir / "sft_train.jsonl", sft_rows(result["sft"]))
    pq.write_table(pa.Table.from_pylist(rl_rows(result["rl"], "rl")), export_dir / "rl.parquet")
    print(f"{len(groups)} distinct call keys; zero shared across SFT and RL")


if __name__ == "__main__":
    main()
