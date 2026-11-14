#!/bin/bash
set -euo pipefail
cd "${TCRL_ROOT:-/root/tcrl}"
export PATH=/root/miniconda3/bin:$PATH PYTHONPATH="$PWD/src" PYTHONUNBUFFERED=1
p=${TCRL_PERSIST:-/root/autodl-fs/tcrl}
data=${TCRL_BFCL_DATA:-$PWD/runs/bfcl_data}
out=${TCRL_BFCL_OUT:-/root/autodl-tmp/runs/d7_bfcl}
score_python=${TCRL_BFCL_PYTHON:-python}
base=$p/models/d3_sft_gold_disjoint_merged
b0=${TCRL_B0:-$p/models/Qwen3-4B-Base}
b1=${TCRL_B1:-$p/models/Qwen3-4B-Instruct-2507}
shots=data/processed/export/sft_train.jsonl
arms=(b0 b1 e0 e0plus e1 e2a e2b e2b_gold_only)
adapters=("" "" "" d5_e0plus_targeted/final d4_grpo_targeted_30step/global_step_30/lora_adapter d6_single_e2a_30step/lora_adapter d6_single_e2b_30step/lora_adapter d6_single_gold_only_30step/lora_adapter)
mkdir -p "$out"
trap 'rc=$?; printf "%s\n" "$rc" > "$out/exit.tmp"; mv "$out/exit.tmp" "$out/exit.txt"' EXIT
(cd "$data" && sha256sum -c "$OLDPWD/scripts/autodl/d7_bfcl.sha256")
"$score_python" -c 'from importlib.metadata import version; from bfcl_eval.eval_checker.ast_eval.ast_checker import ast_checker; assert version("bfcl-eval") == "2026.3.23"'
for model in "$b0" "$b1" "$base"; do test -s "$model/config.json"; done
test -s "$shots"
for adapter in "${adapters[@]:3}"; do test -s "$p/runs/$adapter/adapter_config.json"; done
if [[ ${1:-} == --check ]]; then exit 0; fi
for arm in "${arms[@]}"; do test ! -e "$out/$arm.jsonl"; done
scratch=$(mktemp -d "$out/merge.XXXXXX")
trap 'rc=$?; rmdir "$scratch" 2>/dev/null || true; printf "%s\n" "$rc" > "$out/exit.tmp"; mv "$out/exit.tmp" "$out/exit.txt"' EXIT
for i in "${!arms[@]}"; do
    arm=${arms[i]}
    model=$base
    opts=()
    case "$arm" in
        b0) model=$b0; opts=(--few-shot-data "$shots") ;;
        b1) model=$b1; opts=(--native) ;;
        e0) ;;
        *)
            model="$scratch/$arm"
            python -m tcrl.sft.train_sft --model "$base" --merge "$p/runs/${adapters[i]}" --out "$model"
            ;;
    esac
    python -m tcrl.eval.bfcl generate --data "$data" --model "$model" --out "$out/$arm.jsonl" "${opts[@]}"
    "$score_python" -m tcrl.eval.bfcl score --data "$data" --predictions "$out/$arm.jsonl" --out "$out/$arm.score.json"
    if [[ "$model" == "$scratch/$arm" ]]; then rm -r -- "$model"; fi
done
