"""Create equal-size D7 replay sets using a single shared refusal draw."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random

from tcrl.curriculum.freeze import read, write
from tcrl.data.export import sft_rows

ARMS = ("cs", "cu", "cf")
FROZEN = dict(zip(ARMS, (
    "233652893edee3a99ae37d8a44aeb6568f78f06f5a111de1bcce12623853f022",
    "01797aa8f50729e2d406a10ec7fd650126cd8cd5287b825484bd0bb52acb367e",
    "7245fa8627db6908c1b33e95a4906c84b005d39aa891067813ec1c36f4579527",
), strict=True))


def replay_sets(frozen, static):
    refusals = sorted((row for row in static if row["kind"] == "refuse"), key=lambda row: row["id"])
    if len({row["id"] for row in refusals}) != len(refusals):
        raise ValueError("Duplicate refusal IDs")
    shared = sft_rows(random.Random(20261117).sample(refusals, 53), reverse_frac=0)
    output = {}
    for arm in ARMS:
        calls = frozen[arm]
        combined = calls + shared
        if len(calls) != 300 or len({row["id"] for row in combined}) != 353:
            raise ValueError(f"{arm}: expected 300 frozen calls and 53 distinct replay rows")
        output[arm] = combined
    return output, [row["id"] for row in shared]


def extra_steps(log, budget_seconds):
    final = log[-1]
    if final["step"] != 20 or final["elapsed_s"] <= 0 or budget_seconds <= 0:
        raise ValueError("Expected a completed 20-step C-S log and positive cost")
    return math.floor(budget_seconds / (final["elapsed_s"] / final["step"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("frozen", "static", "out"):
        parser.add_argument(f"--{name}", type=Path, required=False)
    parser.add_argument("--cost-seconds", type=float)
    parser.add_argument("--static-log", type=Path)
    args = parser.parse_args()
    if args.static_log is not None and args.cost_seconds is not None:
        steps = extra_steps(read(args.static_log), args.cost_seconds)
        if steps < 1:
            parser.error("C-F cost is less than one C-S step")
        print(steps)
        return
    if not all((args.frozen, args.static, args.out)) or args.static_log is not None or args.cost_seconds is not None:
        parser.error("provide --frozen/--static/--out or --cost-seconds/--static-log")
    frozen = {}
    for arm, expected in FROZEN.items():
        path = args.frozen / f"{arm}_sft.jsonl"
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"{path}: frozen hash mismatch")
        frozen[arm] = read(path)
    output, ids = replay_sets(frozen, read(args.static))
    args.out.mkdir(parents=True, exist_ok=True)
    for arm, rows in output.items():
        write(args.out / f"{arm}_sft.jsonl", rows)
    (args.out / "refusal_ids.json").write_text(json.dumps(ids, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    main()
