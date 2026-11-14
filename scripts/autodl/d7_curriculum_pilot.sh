#!/bin/bash
set -euo pipefail
cd /root/tcrl
export PATH=/root/miniconda3/bin:$PATH PYTHONPATH=/root/tcrl/src
model=/root/autodl-fs/tcrl/models/d3_sft_gold_disjoint_merged
splits=data/processed/gold_disjoint/splits
seeds=runs/d7_curriculum
out=${TCRL_CURRICULUM_OUT:-/root/autodl-tmp/runs/d7_curriculum_pilot}
seed=${TCRL_CURRICULUM_SEED:-20261114}
draws=${TCRL_CURRICULUM_DRAWS:-1}
mkdir -p "$out"
trap 'printf "%s\n" "$?" > "$out/exit.txt"' EXIT
for arm in cu cf; do
    python -u -m tcrl.curriculum.gen_check --model "$model" --splits "$splits" \
        --seed-file "$seeds/${arm}_pilot_seeds.jsonl" --out "$out/$arm" \
        --few-shot 3 --seed "$seed" --samples-per-seed "$draws"
done
filter_args=(--cu "$out/cu/candidates.jsonl" --cf "$out/cf/candidates.jsonl" --out "$out/filter")
python -m tcrl.curriculum.filter "${filter_args[@]}"
python -u -m tcrl.eval.run_eval --model "$model" --data "$out/filter/candidates.parquet" \
    --out "$out/probe" --n 8 --temperature 1.0 --max-tokens 512 --seed 20261114
python -m tcrl.curriculum.filter "${filter_args[@]}" --scores "$out/probe/samples.jsonl"
