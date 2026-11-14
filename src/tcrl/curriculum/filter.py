"""Export curriculum candidates for probing, then stratify their k=8 results."""
import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

from tcrl.curriculum.diagnostics import read_jsonl
from tcrl.reward import compute_score


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cu", type=Path, required=True)
    ap.add_argument("--cf", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--scores", type=Path)
    args = ap.parse_args()
    records = {
        f"{arm}:{row['id']}": {**row, "id": f"{arm}:{row['id']}", "arm": arm}
        for arm, path in (("cu", args.cu), ("cf", args.cf)) for row in read_jsonl(path)
    }
    args.out.mkdir(parents=True, exist_ok=True)
    if args.scores is None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        from tcrl.data.export import rl_rows

        pq.write_table(pa.Table.from_pylist(rl_rows(list(records.values()), "curriculum")),
                       args.out / "candidates.parquet")
        return

    bins = defaultdict(list)
    seen = set()
    for sample in read_jsonl(args.scores):
        key = sample["id"]
        row = records[key]
        if key in seen or len(sample["outputs"]) != 8:
            raise ValueError("Expected eight outputs per unique candidate")
        seen.add(key)
        passed = sum(compute_score(output, row["gold"], row["tools"])["score"]
                     for output in sample["outputs"])
        tier = "zero" if passed == 0 else "full" if passed == 8 else "mixed"
        bins[row["arm"], tier].append({**row, "pass_count": int(passed), "tier": tier})
    if seen != records.keys():
        raise ValueError("Probe coverage differs from the candidate set")

    rng = random.Random(20261114)
    for rows in bins.values():
        rng.shuffle(rows)
    # Round robin over arm and difficulty, exhausting small strata when needed.
    audit = []
    for index in range(max(map(len, bins.values()), default=0)):
        for key in sorted(bins):
            if index < len(bins[key]) and len(audit) < 100:
                audit.append({**bins[key][index], "semantic_verdict": None})
    if len(audit) != 100:
        raise ValueError("Need at least 100 scored candidates for the semantic audit")
    for path, rows in [("audit.jsonl", audit), *[
        (f"{arm}_{tier}.jsonl", rows) for (arm, tier), rows in bins.items()
    ]]:
        with (args.out / path).open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
