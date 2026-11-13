#!/bin/bash
set -euo pipefail
cd /root/tcrl
export PATH=/root/miniconda3/bin:$PATH
export PYTHONPATH=/root/tcrl/src PYTHONUNBUFFERED=1
disk=/root/autodl-tmp
runs=$disk/runs
base=$disk/models/Qwen3-4B-Base
merged=$disk/models/d3_sft_gold_disjoint_merged
sft=$runs/d3_sft_q3-4b-base_lora_gold_disjoint
check=$runs/d3_merge_gold_disjoint
e0=$runs/d3_e0_gold_disjoint_dev_greedy
p0=$runs/d3_p0_gold_disjoint_rl_probe_n8
dev=data/processed/export/dev.parquet
data=data/processed/gold_disjoint/export
persist=/root/autodl-fs/tcrl

mkdir -p "$sft" "$check" "$e0" "$p0"
trap 'rc=$?; printf "%s\n" "$rc" > "$runs/d3_gold_disjoint_exit.txt"' EXIT
sha256sum "$data/sft_train.jsonl" "$data/rl.parquet" "$dev" > "$runs/d3_gold_disjoint_inputs.txt"
python -m tcrl.sft.train_sft --model "$base" --data "$data/sft_train.jsonl" \
    --out "$sft" --save-steps 50 > "$sft/sft.log" 2>&1
python -m tcrl.sft.train_sft --model "$base" --merge "$sft/final" --out "$merged" > "$check/merge.log" 2>&1
python -m tcrl.sft.check_merge --base "$base" --adapter "$sft/final" --merged "$merged" \
    --data "$dev" --out "$check" > "$check/check.log" 2>&1
python -m tcrl.eval.run_eval --model "$merged" --data "$dev" --out "$e0" > "$e0/eval.log" 2>&1
python -m tcrl.eval.run_eval --model "$merged" --data "$data/rl.parquet" --out "$p0" \
    --n 8 --temperature 1.0 --max-tokens 512 --max-prompt-tokens 4096 > "$p0/eval.log" 2>&1
mkdir -p "$persist/models" "$persist/runs/$(basename "$sft")"
cp -a "$merged" "$persist/models/"
cp -a "$sft/final" "$persist/runs/$(basename "$sft")/"
