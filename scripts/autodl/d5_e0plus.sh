#!/bin/bash
set -euo pipefail
cd /root/tcrl
export PATH=/root/miniconda3/bin:$PATH PYTHONPATH=/root/tcrl/src PYTHONUNBUFFERED=1
p=/root/autodl-fs/tcrl
base=$p/models/d3_sft_gold_disjoint_merged
grpo=$p/runs/d4_grpo_targeted_30step/global_step_30/lora_adapter
r=/root/autodl-tmp/runs/d5_e0plus_targeted
m=/root/autodl-tmp/models
sft=data/processed/gold_disjoint/export/sft_targeted.jsonl
dev=data/processed/export/dev.parquet
mkdir -p "$r" "$m"
trap 'rc=$?; printf "%s\n" "$rc" > "$r/exit.tmp"; mv "$r/exit.tmp" "$r/exit.txt"' EXIT
sha256sum -c <<EOF
2bf32d58bb9634451425638b7f4cb360939e0697c92bb2ba0c7934ed55530ea6  $sft
0d862fe5e4c5203cbea1963e47cf90d8cd121b937d870f1c489ce0383f27bc1c  $dev
EOF
python -m tcrl.sft.train_sft --model "$base" --data "$sft" --out "$r/sft" \
    --max-steps 30 --batch-size 8 --grad-accum 4 --lr 1e-4 --save-steps 15
python -m tcrl.sft.train_sft --model "$base" --merge "$r/sft/final" --out "$m/d5_e0plus"
python -m tcrl.sft.train_sft --model "$base" --merge "$grpo" --out "$m/d5_grpo30"
for arm in e0 e0plus grpo30; do
    model="$m/d5_$arm"
    if [ "$arm" = e0 ]; then model="$base"; fi
    python -m tcrl.eval.run_eval --model "$model" --data "$dev" --out "$r/$arm"
done
mkdir -p "$p/runs/d5_e0plus_targeted"
cp -a "$r/sft/final" "$p/runs/d5_e0plus_targeted/"
