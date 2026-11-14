"""Freeze equal-sized D7 solve-only curricula from the combined pilot."""
import argparse
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import json
from pathlib import Path
import random
import re
import unicodedata

from tcrl.data.export import sft_rows


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf8").splitlines()]


def write(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf8")


def gold_key(row):
    return tuple(sorted(json.dumps(call, sort_keys=True, ensure_ascii=False) for call in row["gold"]))


def normalized(question):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", question).casefold()))


def dedup(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[gold_key(row)].append(row)
    kept = []
    for group in groups.values():
        pending = sorted(group, key=lambda row: (row["round"], row["id"]))
        while pending:
            component = [pending.pop(0)]
            for member in component:
                query = normalized(member["question"])
                neighbors = [
                    other for other in pending
                    if SequenceMatcher(None, query, normalized(other["question"]), autojunk=False).ratio() >= 0.90
                ]
                component.extend(neighbors)
                pending = [other for other in pending if other not in neighbors]
            if len({row["arm"] for row in component}) == 1:
                kept.append(component[0])
    return kept


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidates", "static", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--size", type=int, default=300)
    args = parser.parse_args()
    candidates = read(args.candidates)
    assert all(0 <= row["pass_count"] <= 8 for row in candidates)
    retained = dedup(candidates)
    pools = {arm: [row for row in retained if row["arm"] == arm and row["pass_count"] > 0] for arm in ("cu", "cf")}
    static = [row for row in read(args.static) if row["kind"] == "call"]
    size = min(args.size, len(static), *(len(pool) for pool in pools.values()))
    rng = random.Random(20261116)
    selected = {"cs": rng.sample(static, size)}
    for arm, pool in pools.items():
        mixed = [row for row in pool if row["pass_count"] < 8]
        full = [row for row in pool if row["pass_count"] == 8]
        if len(mixed) > size:
            raise ValueError(f"{arm}: {len(mixed)} mixed questions exceed size {size}")
        selected[arm] = mixed + rng.sample(full, size - len(mixed))
    args.out.mkdir(parents=True, exist_ok=True)
    for arm, rows in selected.items():
        write(args.out / f"{arm}_sft.jsonl", sft_rows(rows, reverse_frac=0))
        print(arm, "selected", len(rows), dict(Counter(row.get("tier", "static") for row in rows)))
    print("retained", dict(Counter((row["arm"], row["tier"]) for row in retained)))


if __name__ == "__main__":
    main()
