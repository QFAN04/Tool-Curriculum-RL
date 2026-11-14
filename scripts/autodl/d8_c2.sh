#!/bin/bash
set -euo pipefail
cd /root/tcrl
export PATH=/root/miniconda3/bin:$PATH PYTHONPATH="$PWD/src" PYTHONUNBUFFERED=1
root=/root/d8_c2
p=/root/autodl-fs/tcrl
base=$p/models/d3_sft_gold_disjoint_merged
splits=data/processed/gold_disjoint/splits
input=/root/d8_c2_inputs
mkdir -p "$root"
trap 'rc=$?; echo "$rc" > "$root/exit.tmp"; mv "$root/exit.tmp" "$root/exit.txt"' EXIT
test ! -e "$root/merged"
python -m tcrl.sft.train_sft --model "$base" --merge "$p/runs/d8_curriculum_replay/cf" --out "$root/merged"
python -m tcrl.eval.run_eval --model "$root/merged" --data "$input/rl.parquet" --out "$root/rl" \
    --n 8 --temperature 1 --seed 20261114
python -m tcrl.curriculum.seeds --sft "$splits/sft.jsonl" --rl "$splits/rl.jsonl" \
    --samples "$root/rl/samples.jsonl" --out "$root/eligible.jsonl"
python - "$root" <<'PY'
import json, random, sys
from pathlib import Path
from tcrl.curriculum.freeze import read, write
root = Path(sys.argv[1])
pool = [r for r in read(root / "eligible.jsonl") if any(c["arguments"] for c in r["gold"])]
random.Random(20261114).shuffle(pool)
pool.sort(key=lambda r: r["multi_call"] + bool(r["low_frequency_families"]), reverse=True)
if len(pool) < 200:
    raise ValueError(f"Only {len(pool)} eligible C2 seeds")
write(root / "seeds.jsonl", pool[:200])
PY
python -m tcrl.curriculum.gen_check --model "$root/merged" --splits "$splits" \
    --seed-file "$root/seeds.jsonl" --out "$root/generated" --few-shot 3 --seed 20261114
python - "$root" <<'PY'
import sys
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
from tcrl.curriculum.freeze import dedup, read, write
from tcrl.data.export import rl_rows
root = Path(sys.argv[1])
rows = dedup([{**r, "arm": "cf", "round": 2} for r in read(root / "generated/candidates.jsonl")])
if not rows:
    raise ValueError("No grounded C2 candidates")
write(root / "generated/dedup.jsonl", rows)
pq.write_table(pa.Table.from_pylist(rl_rows(rows, "curriculum")), root / "c2.parquet")
PY
for pair in c2_cf c2_e0 c1_cf; do
    model="$root/merged"
    data="$root/c2.parquet"
    case "$pair" in
        c2_e0) model="$base" ;;
        c1_cf) data="$input/c1_cf.parquet" ;;
    esac
    python -m tcrl.eval.run_eval --model "$model" --data "$data" --out "$root/$pair" \
        --n 8 --temperature 1 --seed 20261114
done
rm -r -- "$root/merged"
tar -czf "$p/runs/d8-c2-17346.tar.gz.tmp" -C "$root" rl eligible.jsonl seeds.jsonl generated c2_cf c2_e0 c1_cf
mv "$p/runs/d8-c2-17346.tar.gz.tmp" "$p/runs/d8-c2-17346.tar.gz"
