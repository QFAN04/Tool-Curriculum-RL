#!/bin/bash
set -euo pipefail
cd "${TCRL_ROOT:-/root/tcrl}"
export PATH=/root/miniconda3/bin:$PATH PYTHONPATH="$PWD/src" PYTHONUNBUFFERED=1
p=${TCRL_PERSIST:-/root/autodl-fs/tcrl}
base=$p/models/d3_sft_gold_disjoint_merged
r=${TCRL_D8_OUT:-/root/autodl-tmp/runs/d8_curriculum_replay}
bfcl=${TCRL_BFCL_DATA:-$PWD/runs/bfcl_data}
score_python=${TCRL_BFCL_PYTHON:-python}
dev=data/processed/export/dev.parquet
cf_seconds=${TCRL_CF_GPU_SECONDS:?set from the reconstructed C-F generation and screening GPU time}
[[ "$cf_seconds" =~ ^[1-9][0-9]*$ ]]
test -s "$base/model.safetensors.index.json"
mkdir -p "$r"
trap 'rc=$?; printf "%s\n" "$rc" > "$r/exit.tmp"; mv "$r/exit.tmp" "$r/exit.txt"' EXIT
sha256sum -c <<EOF
0d862fe5e4c5203cbea1963e47cf90d8cd121b937d870f1c489ce0383f27bc1c  $dev
7f6b2f29301563185cf88a118df50fa381a98a5bace14a156c8b2117182e64ee  data/processed/gold_disjoint/splits/sft.jsonl
EOF
(cd "$bfcl" && sha256sum -c "$OLDPWD/scripts/autodl/d7_bfcl.sha256")
"$score_python" -c 'from importlib.metadata import version; from bfcl_eval.eval_checker.ast_eval.ast_checker import ast_checker; assert version("bfcl-eval") == "2026.3.23"'
python -m tcrl.curriculum.replay --frozen runs/d7_curriculum_frozen \
    --static data/processed/gold_disjoint/splits/sft.jsonl --out "$r/data"
if [[ ${1:-} == --check ]]; then exit 0; fi
for arm in cs cu cf csplus; do test ! -e "$r/$arm"; done
for arm in cs cu cf csplus; do
    run="$r/$arm"
    mkdir -p "$run"
    opts=()
    steps=20
    data="$r/data/${arm}_sft.jsonl"
    if [[ "$arm" == csplus ]]; then
        mkdir -p "$run/sft"
        cp -a "$r/cs/sft/step-20" "$run/sft/"
        data="$r/data/cs_sft.jsonl"
        extra=$(python -m tcrl.curriculum.replay --cost-seconds "$cf_seconds" --static-log "$r/cs/sft/train_log.jsonl")
        printf '%s\n' "$cf_seconds" > "$run/cf_gpu_seconds.txt"
        printf '%s\n' "$extra" > "$run/extra_steps.txt"
        steps=$((20 + extra))
        opts=(--resume --tail-lr 1e-5)
    fi
    python -m tcrl.sft.train_sft --model "$base" --data "$data" --out "$run/sft" \
        --max-steps "$steps" --batch-size 8 --grad-accum 4 --lr 2e-5 --save-steps 20 "${opts[@]}"
    dest="$p/runs/$(basename "$r")/$arm"
    mkdir -p "$dest"
    cp -a "$run/sft/final/." "$dest/"
    diff -qr "$run/sft/final" "$dest"
    python -m tcrl.sft.train_sft --model "$base" --merge "$dest" --out "$run/merged"
    python -m tcrl.eval.run_eval --model "$run/merged" --data "$dev" --out "$run/dev"
    python -m tcrl.eval.bfcl generate --data "$bfcl" --model "$run/merged" --out "$run/bfcl.jsonl"
    "$score_python" -m tcrl.eval.bfcl score --data "$bfcl" --predictions "$run/bfcl.jsonl" --out "$run/bfcl.score.json"
    rm -r -- "$run/merged"
done
