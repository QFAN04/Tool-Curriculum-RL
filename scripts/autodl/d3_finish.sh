#!/bin/bash
set -euo pipefail
cd /root/tcrl
export PATH=/root/miniconda3/bin:$PATH
export PYTHONPATH=/root/tcrl/src PYTHONUNBUFFERED=1
disk=/root/autodl-tmp
runs=$disk/runs
base=$disk/models/Qwen3-4B-Base
merged=$disk/models/d3_sft_merged
sft=$runs/d3_sft_q3-4b-base_lora_v2
data=data/processed/export

until grep -qx done "$sft/sft.log"; do
    if ! pgrep -f '^/root/miniconda3/bin/python -m tcrl.sft.train_sft' >/dev/null; then
        tail -40 "$sft/sft.log"
        exit 1
    fi
    sleep 10
done

mkdir -p "$runs/d3_merge_check"
python -m tcrl.sft.train_sft --model "$base" --merge "$sft/final" --out "$merged" > "$runs/d3_merge_check/merge.log" 2>&1
python -m tcrl.sft.check_merge --base "$base" --adapter "$sft/final" --merged "$merged" \
    --data "$data/dev.parquet" --out "$runs/d3_merge_check" > "$runs/d3_merge_check/check.log" 2>&1

eval_run() {
    local run=$1
    shift
    mkdir -p "$runs/$run"
    python -m tcrl.eval.run_eval --model "$merged" --out "$runs/$run" "$@" > "$runs/$run/eval.log" 2>&1
}
eval_run d3_e0_sft_dev_greedy --data "$data/dev.parquet"
eval_run d3_p0_sft_rl_probe_n8 --data "$data/rl.parquet" --n 8 --temperature 1.0 --max-tokens 512 --max-prompt-tokens 4096
mkdir -p "$runs/d3_gen_check"
python -m tcrl.curriculum.gen_check --model "$merged" --out "$runs/d3_gen_check" \
    --n 50 --few-shot 3 > "$runs/d3_gen_check/gen.log" 2>&1
