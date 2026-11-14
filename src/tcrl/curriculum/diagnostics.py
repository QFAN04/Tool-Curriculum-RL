"""Report training-tail coverage and tool-family combination entropy."""
import argparse
import json
import math
from collections import Counter
from pathlib import Path

from tcrl.data.split import gold_families


def read_jsonl(path):
    with path.open(encoding="utf-8") as handle:
        return list(map(json.loads, handle))


def coverage(records, frequencies):
    combinations = Counter(tuple(sorted(gold_families(row))) for row in records)
    families = {family for combo in combinations for family in combo}
    unseen = families - frequencies.keys()
    if unseen:
        raise ValueError(f"Generated calls contain families outside training: {sorted(unseen)}")
    total = len(records)
    return {
        "samples": total,
        "families": len(families),
        "low_frequency_families": sorted(f for f in families if frequencies[f] < 10),
        "low_frequency_count": sum(frequencies[f] < 10 for f in families),
        "combination_entropy_bits": -sum(
            (count / total) * math.log2(count / total) for count in combinations.values()
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("sft", "rl", "cu", "cf"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    frequencies = Counter(
        family for path in (args.sft, args.rl) for row in read_jsonl(path)
        for family in gold_families(row)
    )
    print(json.dumps({
        "C-U": coverage(read_jsonl(args.cu), frequencies),
        "C-F": coverage(read_jsonl(args.cf), frequencies),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
