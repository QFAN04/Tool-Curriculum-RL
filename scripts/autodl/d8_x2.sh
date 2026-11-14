#!/bin/bash
# X2 changes only E1's training reward. Independent eval uses the original scorer.
set -euo pipefail
cd /root/tcrl
export PATH=/root/miniconda3/bin:$PATH PYTHONPATH="$PWD/src" PYTHONUNBUFFERED=1
run=d8_x2_graded_30step
out=/root/autodl-tmp/runs/$run
persist=/root/autodl-fs/tcrl
base=$persist/models/d3_sft_gold_disjoint_merged
bfcl=${TCRL_BFCL_DATA:-$PWD/runs/bfcl_data}
score_python=${TCRL_BFCL_PYTHON:-python}
(cd "$bfcl" && sha256sum -c "$OLDPWD/scripts/autodl/d7_bfcl.sha256")
"$score_python" -c 'from importlib.metadata import version; from bfcl_eval.eval_checker.ast_eval.ast_checker import ast_checker; assert version("bfcl-eval") == "2026.3.23"'
if [[ ${1:-} == --check ]]; then
    bash -n scripts/autodl/d4_grpo.sh
    test -s /root/SDPO/verl/trainer/main_ppo.py
    test -s "$base/model.safetensors.index.json"
    exit 0
fi
test ! -e "$out/train.log"
bash scripts/autodl/d4_grpo.sh "$run" \
    custom_reward_function.path=/root/tcrl/src/tcrl/verl_reward_graded.py
adapter="$out/ckpt/$run/global_step_30/actor/lora_adapter"
dest="$persist/runs/$run/lora_adapter"
test -s "$adapter/adapter_model.safetensors"
mkdir -p "$dest"
cp -a "$adapter/." "$dest/"
diff -qr "$adapter" "$dest"
python -m tcrl.sft.train_sft --model "$base" --merge "$dest" --out "$out/merged"
python -m tcrl.eval.run_eval --model "$out/merged" --data data/processed/export/dev.parquet --out "$out/dev"
python -m tcrl.eval.run_eval --model "$out/merged" --data data/processed/gold_disjoint/export/rl_targeted.parquet \
    --out "$out/probe" --n 8 --temperature 1.0 --seed 20261114
python -m tcrl.eval.bfcl generate --data "$bfcl" --model "$out/merged" --out "$out/bfcl.jsonl"
"$score_python" -m tcrl.eval.bfcl score --data "$bfcl" --predictions "$out/bfcl.jsonl" --out "$out/bfcl.score.json"
rm -r -- "$out/merged"
