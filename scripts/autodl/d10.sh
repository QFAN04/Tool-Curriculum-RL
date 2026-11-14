#!/bin/bash
set -euo pipefail
cd "${TCRL_ROOT:-/root/tcrl}"
export PATH=/root/miniconda3/bin:$PATH PYTHONPATH="$PWD/src" PYTHONUNBUFFERED=1
p=${TCRL_PERSIST:-/root/autodl-fs/tcrl}
d9=${TCRL_D9_DATA:-$PWD/runs/d9_stage0}
gate=${TCRL_D9_APPROVED_GATE:-$p/runs/d9-direct-gate-43482.tar.gz}
bfcl=${TCRL_BFCL_DATA:-$PWD/runs/bfcl_data}
score_python=${TCRL_BFCL_PYTHON:-python}
base=$p/models/d3_sft_gold_disjoint_merged
dest=$p/runs/d10_curriculum
disk=${TCRL_D10_SCRATCH:-/root/autodl-tmp}
mode=${1:---check}
case "$mode" in --check|--run|--resume) ;; *) exit 2 ;; esac
sha256sum -c <(printf '%s  %s\n' \
    c47feddf4e9592835012f86ea0582ebdec2666135ee05a7589489a4a09c486d6 "$d9/manifest.json" \
    6104f73638302e63476ce8d15601d981047a7d4659ca46bbbd5214da8207872b "$gate")
(cd "$bfcl" && sha256sum -c "$OLDPWD/scripts/autodl/d7_bfcl.sha256")
"$score_python" -c 'from importlib.metadata import version; from bfcl_eval.eval_checker.ast_eval.ast_checker import ast_checker; assert version("bfcl-eval") == "2026.3.23"'
test -s "$base/model.safetensors.index.json"
free_kib=$(df -Pk "$disk" | awk 'NR==2 {print $4}')
(( free_kib >= 18 * 1024 * 1024 )) || { echo 'D10 needs 18 GiB free for one staged base and one merge.' >&2; exit 1; }
persist_kib=$(df -Pk "$p/runs" | awk 'NR==2 {print $4}')
(( persist_kib >= 8 * 1024 * 1024 )) || { echo 'D10 needs 8 GiB free for adapters and SFT checkpoints.' >&2; exit 1; }
if [[ "$mode" == --check ]]; then exit 0; fi
if [[ "$mode" == --run ]]; then mkdir "$dest"; else test -d "$dest"; fi
exec 9>"$dest/.lock"
flock -n 9
if [[ -f "$dest/exit.txt" ]] && [[ $(cat "$dest/exit.txt") == 0 ]]; then exit 0; fi
scratch=$(mktemp -d "$disk/d10.XXXXXX")
finish() {
    local rc=$?
    trap - EXIT
    set +e
    rm -rf -- "$scratch"
    printf '%s\n' "$rc" > "$dest/run_rc.txt"
    archive="$dest.tar.gz"
    if tar -C "$(dirname "$dest")" --exclude=.lock --exclude='step-*' --exclude='*/train/final' \
        -czf "$archive.tmp" "$(basename "$dest")" &&
        gzip -t "$archive.tmp" && mv "$archive.tmp" "$archive" && sha256sum "$archive" > "$archive.sha256"; then
        status=$rc
    else
        status=1
    fi
    printf '%s\n' "$status" > "$dest/exit.tmp"
    mv "$dest/exit.tmp" "$dest/exit.txt"
    exit "$status"
}
trap finish EXIT
rm -f -- "$dest/exit.txt"
phase() { printf '%s\n' "$1" > "$dest/phase.txt"; }
phase stage_base
date -Is > "$dest/start_time.txt"
cp -aL "$base" "$scratch/base"
base=$scratch/base
mkdir -p "$dest/data"
data="$dest/data"
build=(python -m tcrl.curriculum.d10)
run_build() { "${build[@]}" "$1" --data "$d9" --archive "$gate" --out "$data" "${@:2}"; }
if [[ ! -f "$data/generate.done" ]]; then
    phase generate
    run_build prepare > "$data/pool.log" 2>&1
    run_build generate --model "$base" > "$data/generate.log" 2>&1
    touch "$data/generate.done"
fi
if [[ ! -s "$data/probe/summary.json" ]]; then
    phase screen
    python -m tcrl.eval.run_eval --model "$base" --data "$data/generated.parquet" --out "$data/probe" \
        --n 8 --temperature 1 --top-p 1 --top-k -1 --max-tokens 512 --seed 20261119 > "$data/probe.log" 2>&1
fi
run_build freeze --probe "$data/probe/samples.jsonl"
for arm in cs cu cf; do
    phase "train_$arm"
    train="$dest/$arm/train"
    if [[ ! -f "$train/done" ]]; then
        mkdir -p "$train"
        python -m tcrl.sft.train_sft --model "$base" --data "$data/${arm}_sft.jsonl" --out "$train" \
            --max-steps 30 --batch-size 8 --grad-accum 4 --lr 3e-5 --save-steps 10 --resume > "$train/sft.log" 2>&1
        grep -q 'examples 424 (dropped 0), optimizer steps 30' "$train/sft.log"
        test -s "$train/final/adapter_model.safetensors"
        test -s "$train/final/special_tokens.pt"
        (cd "$train/final" && sha256sum adapter_model.safetensors adapter_config.json special_tokens.pt) > "$train/adapter.sha256"
        touch "$train/done"
    fi
done
for arm in e0 cs cu cf; do
    phase "eval_$arm"
    out="$dest/$arm/eval"
    if [[ -f "$out/done" ]]; then continue; fi
    mkdir -p "$out"
    model=$base
    if [[ "$arm" != e0 ]]; then
        model=$scratch/merged
        python -m tcrl.sft.train_sft --model "$base" --merge "$dest/$arm/train/final" --out "$model" > "$out/merge.log" 2>&1
    fi
    for split in heldout external_dev541; do
        python -m tcrl.eval.run_eval --model "$model" --data "$d9/$split.parquet" --out "$out/$split" \
            --temperature 0 --max-tokens 1024 > "$out/$split.log" 2>&1
    done
    python -m tcrl.eval.d9 paired --samples "$out/heldout/samples.jsonl" --pairs "$d9/heldout_pairs.jsonl" --out "$out/paired.json"
    python -m tcrl.eval.bfcl generate --data "$bfcl" --model "$model" --out "$out/bfcl.jsonl" \
        --temperature 0 --max-tokens 1024 > "$out/bfcl_generate.log" 2>&1
    "$score_python" -m tcrl.eval.bfcl score --data "$bfcl" --predictions "$out/bfcl.jsonl" \
        --out "$out/bfcl.score.json" > "$out/bfcl_score.log" 2>&1
    touch "$out/done"
    if [[ "$arm" != e0 ]]; then rm -r -- "$model"; fi
done
phase summarize
python -m tcrl.eval.d10 --run "$dest" --pairs "$d9/heldout_pairs.jsonl"
phase complete
date -Is > "$dest/finish_time.txt"
