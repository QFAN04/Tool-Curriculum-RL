#!/bin/bash
set -euo pipefail
cd "${TCRL_ROOT:-/root/tcrl}"
export PATH=/root/miniconda3/bin:$PATH PYTHONPATH="$PWD/src" PYTHONUNBUFFERED=1
p=${TCRL_PERSIST:-/root/autodl-fs/tcrl}
# A single consolidated snapshot can be staged on an instance with another region's disk.
weights=${TCRL_D12_WEIGHTS:-$p}
out=${TCRL_D12_OUT:-$p/runs/d12_final}
disk=${TCRL_D12_SCRATCH:-/root/autodl-tmp}
models=scripts/autodl/d12_models.tsv
manifest=data/manifests/splits.json
source=data/processed/export
mode=${1:---check}
case "$mode" in --check|--run|--resume) ;; *) exit 2 ;; esac
sha256sum -c scripts/autodl/d12_inputs.sha256
python -m tcrl.eval.d12 check-models --models "$models"
free_kib=$(df -Pk "$disk" | awk 'NR==2 {print $4}')
(( free_kib >= 18 * 1024 * 1024 )) || { echo "D12 needs 18 GiB free for one staged base and one merge." >&2; exit 1; }
missing=0
declare -A checked_bases=()
require_file() {
    if [[ ! -s "$1" ]]; then echo "Missing D12 input: $1" >&2; missing=1; fi
}
while IFS=$'\t' read -r id n8 base adapter prompt; do
    if [[ ! -v checked_bases[$base] ]]; then
        checked_bases[$base]=1
        require_file "$weights/$base/config.json"
        require_file "$weights/$base/tokenizer_config.json"
        require_file "$weights/$base/model.safetensors.index.json"
        if [[ -s "$weights/$base/model.safetensors.index.json" ]]; then
            shards=$(python -c 'import json,sys;print("\n".join(sorted(set(json.load(open(sys.argv[1]))["weight_map"].values()))))' \
                "$weights/$base/model.safetensors.index.json")
            while IFS= read -r shard; do require_file "$weights/$base/$shard"; done <<< "$shards"
            if [[ -z "$shards" ]]; then echo "Empty shard map: $weights/$base" >&2; missing=1; fi
        fi
    fi
    if [[ "$adapter" != - ]]; then
        require_file "$weights/$adapter/adapter_config.json"
        require_file "$weights/$adapter/adapter_model.safetensors"
        case "$id" in
            e0plus|d7_*|d8_*|d9_sft|d10_cf) require_file "$weights/$adapter/special_tokens.pt" ;;
        esac
    fi
done < <(tail -n +2 "$models")
(( missing == 0 ))
# Match the D10 winner against the hashes archived at training completion.
(
    cd "$weights"
    sha256sum -c "$OLDPWD/scripts/autodl/d12_d10_cf.sha256"
)
if [[ "$mode" == --check ]]; then exit 0; fi

mkdir -p "$(dirname "$out")"
if [[ "$mode" == --run ]]; then
    mkdir "$out"
    cp "$models" "$out/models.tsv"
    cp scripts/autodl/d12_inputs.sha256 "$out/inputs.sha256"
    cp scripts/autodl/d12_d10_cf.sha256 "$out/d10_cf.sha256"
else
    cmp "$models" "$out/models.tsv"
    cmp scripts/autodl/d12_inputs.sha256 "$out/inputs.sha256"
    cmp scripts/autodl/d12_d10_cf.sha256 "$out/d10_cf.sha256"
    if [[ -e "$out/exit.txt" ]] && [[ $(cat "$out/exit.txt") == 0 ]]; then
        echo "D12 is already complete."
        exit 0
    fi
fi
exec 9>"$out/.lock"
flock -n 9
scratch=$(mktemp -d "$disk/d12.XXXXXX")
finish() {
    local rc=$?
    rm -rf -- "$scratch"
    printf '%s\n' "$rc" > "$out/exit.tmp"
    mv "$out/exit.tmp" "$out/exit.txt"
}
trap finish EXIT
rm -f -- "$out/exit.txt"
python -m tcrl.eval.d12 prepare --models "$models" --manifest "$manifest" \
    --source "$source" --data "$scratch/test.parquet"
python -VV > "$out/environment.txt"
python -m pip show vllm transformers torch numpy pyarrow >> "$out/environment.txt"
nvidia-smi >> "$out/environment.txt"
staged=
while IFS=$'\t' read -r id n8 base adapter prompt; do
    arm="$out/$id"
    if [[ -f "$arm/done" ]]; then continue; fi
    if [[ -e "$arm" ]]; then mv "$arm" "$arm.incomplete.$(date +%s%N)"; fi
    mkdir "$arm"
    printf '%s\n' "$id" > "$out/phase.txt"
    if [[ "$staged" != "$base" ]]; then
        if [[ -d "$scratch/base" ]]; then rm -r -- "$scratch/base"; fi
        cp -aL "$weights/$base" "$scratch/base"
        key=${base##*/}
        (cd "$scratch/base" && find . -maxdepth 1 -type f -print0 | sort -z | xargs -0 sha256sum) > "$scratch/base.sha256"
        if [[ -f "$out/$key.sha256" ]]; then
            cmp "$scratch/base.sha256" "$out/$key.sha256"
        else
            cp "$scratch/base.sha256" "$out/$key.sha256"
        fi
        staged="$base"
    fi
    model="$scratch/base"
    if [[ "$adapter" != - ]]; then
        model="$scratch/merged"
        (
            cd "$weights/$adapter"
            sha256sum adapter_config.json adapter_model.safetensors
            if [[ -f special_tokens.pt ]]; then sha256sum special_tokens.pt; fi
        ) > "$arm/adapter.sha256"
        python -m tcrl.sft.train_sft --model "$scratch/base" --merge "$weights/$adapter" \
            --out "$model" > "$arm/merge.log" 2>&1
    fi
    options=()
    case "$prompt" in
        native) options+=(--native-tools) ;;
        fewshot3) options+=(--few-shot 3 --few-shot-data "$source/sft_train.jsonl") ;;
    esac
    if [[ "$n8" == 1 ]]; then options+=(--also-sample-eight-to "$arm/sample8"); fi
    python -m tcrl.eval.run_eval --model "$model" --data "$scratch/test.parquet" \
        --out "$arm/greedy" --n 1 --temperature 0 --top-p 1 --top-k -1 --max-tokens 1024 \
        --seed 20261113 "${options[@]}" > "$arm/eval.log" 2>&1
    test -s "$arm/greedy/summary.json"
    if [[ "$n8" == 1 ]]; then test -s "$arm/sample8/summary.json"; fi
    touch "$arm/done"
    if [[ "$adapter" != - ]]; then rm -r -- "$model"; fi
done < <(tail -n +2 "$models")
printf '%s\n' summarize > "$out/phase.txt"
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m tcrl.eval.d12 summarize --models "$models" \
    --manifest "$manifest" --root "$out" --data "$scratch/test.parquet"
archive="$out.tar.gz"
tar -C "$(dirname "$out")" --exclude=.lock --exclude='*.incomplete.*' --exclude=exit.txt \
    -czf "$archive.tmp" "$(basename "$out")"
gzip -t "$archive.tmp"
mv "$archive.tmp" "$archive"
sha256sum "$archive" > "$archive.sha256"
printf '%s\n' complete > "$out/phase.txt"
