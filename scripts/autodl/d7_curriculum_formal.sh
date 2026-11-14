#!/bin/bash
set -euo pipefail
cd "${TCRL_ROOT:-/root/tcrl}"
export PATH=/root/miniconda3/bin:$PATH PYTHONPATH="$PWD/src" PYTHONUNBUFFERED=1
p=${TCRL_PERSIST:-/root/autodl-fs/tcrl}
base=$p/models/d3_sft_gold_disjoint_merged
r=${TCRL_D7_OUT:-/root/autodl-tmp/runs/d7_curriculum_formal}
f=runs/d7_curriculum_frozen
dev=data/processed/export/dev.parquet
target=data/processed/gold_disjoint/export/rl_targeted.parquet
seeds=runs/d7_curriculum
bfcl=${TCRL_BFCL_DATA:-$PWD/runs/bfcl_data}
score_python=${TCRL_BFCL_PYTHON:-python}
mkdir -p "$r"
trap 'rc=$?; printf "%s\n" "$rc" > "$r/exit.tmp"; mv "$r/exit.tmp" "$r/exit.txt"' EXIT
sha256sum -c <<EOF
233652893edee3a99ae37d8a44aeb6568f78f06f5a111de1bcce12623853f022  $f/cs_sft.jsonl
01797aa8f50729e2d406a10ec7fd650126cd8cd5287b825484bd0bb52acb367e  $f/cu_sft.jsonl
7245fa8627db6908c1b33e95a4906c84b005d39aa891067813ec1c36f4579527  $f/cf_sft.jsonl
0d862fe5e4c5203cbea1963e47cf90d8cd121b937d870f1c489ce0383f27bc1c  $dev
0f9c0404f6d557dabd0c79f5a0369b311f1dd4e51502b6063c5255233f3f8119  $target
3503f9c049488aa2f9f6522c23fb2c1439eaba284d737cb88ab91d707123c195  $seeds/cu_pilot_seeds.jsonl
da56559b45f4ab39b3deffa7a640627d45041da02801f8ac577f3e82af505b55  $seeds/cf_pilot_seeds.jsonl
EOF
for file in "$target" "$seeds/cu_pilot_seeds.jsonl" "$seeds/cf_pilot_seeds.jsonl" "$base/config.json"; do
    test -s "$file"
done
(cd "$bfcl" && sha256sum -c "$OLDPWD/scripts/autodl/d7_bfcl.sha256")
"$score_python" -c 'from importlib.metadata import version; from bfcl_eval.eval_checker.ast_eval.ast_checker import ast_checker; assert version("bfcl-eval") == "2026.3.23"'
if [[ ${1:-} == --check ]]; then exit 0; fi
if [[ -e "$r/cs" || -e "$r/cu" || -e "$r/cf" ]]; then
    echo "Use a new TCRL_D7_OUT for a fresh run" >&2
    exit 2
fi
python -m tcrl.eval.run_eval --model "$base" --data "$dev" --out "$r/e0/dev"
for arm in cs cu cf; do
    run="$r/$arm"
    python -m tcrl.sft.train_sft --model "$base" --data "$f/${arm}_sft.jsonl" --out "$run/sft" \
        --max-steps 30 --batch-size 8 --grad-accum 4 --lr 1e-4 --save-steps 15
    dest="$p/runs/$(basename "$r")/$arm"
    mkdir -p "$dest"
    cp -a "$run/sft/final/." "$dest/"
    diff -qr "$run/sft/final" "$dest"
    python -m tcrl.sft.train_sft --model "$base" --merge "$dest" --out "$run/merged"
    python -m tcrl.eval.run_eval --model "$run/merged" --data "$dev" --out "$run/dev"
    if [[ "$arm" != cs ]]; then
        python -m tcrl.eval.run_eval --model "$run/merged" --data "$target" --out "$run/probe" \
            --n 8 --temperature 1.0 --seed 20261114
    fi
    python -m tcrl.eval.bfcl generate --data "$bfcl" --model "$run/merged" --out "$run/bfcl.jsonl"
    "$score_python" -m tcrl.eval.bfcl score --data "$bfcl" --predictions "$run/bfcl.jsonl" --out "$run/bfcl.score.json"
    rm -r -- "$run/merged"
done
python -m tcrl.curriculum.probe_strata --target "$target" \
    --cu-seeds "$seeds/cu_pilot_seeds.jsonl" --cf-seeds "$seeds/cf_pilot_seeds.jsonl" \
    --cu "$r/cu/probe/samples.jsonl" --cf "$r/cf/probe/samples.jsonl" --out "$r/probe_strata.json"
