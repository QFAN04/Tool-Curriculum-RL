#!/bin/bash
# Run from the AutoDL deployment; all generated evaluation fixtures are frozen locally.
set -euo pipefail
cd /root/tcrl
export PATH=/root/miniconda3/bin:$PATH PYTHONPATH="$PWD/src" PYTHONUNBUFFERED=1
data=${TCRL_D9_DATA:-$PWD/runs/d9_stage0}
bfcl=${TCRL_BFCL_DATA:-$PWD/runs/bfcl_data}
score_python=${TCRL_BFCL_PYTHON:-python}
p=${TCRL_PERSIST:-/root/autodl-fs/tcrl}
out=${TCRL_D9_OUT:-/root/autodl-tmp/runs/d9}
e0=$p/models/d3_sft_gold_disjoint_merged
mode=${1:?choose check, thinking or methods}
mkdir -p "$out"
python - "$data" <<'PY'
import hashlib, json, sys
from pathlib import Path
p = Path(sys.argv[1])
for name, digest in json.loads((p / "manifest.json").read_text())["sha256"].items():
    if hashlib.sha256((p / name).read_bytes()).hexdigest() != digest:
        raise SystemExit(f"D9 input differs: {name}")
PY
(cd "$bfcl" && sha256sum -c "$OLDPWD/scripts/autodl/d7_bfcl.sha256")
"$score_python" -c 'from importlib.metadata import version; from bfcl_eval.eval_checker.ast_eval.ast_checker import ast_checker; assert version("bfcl-eval") == "2026.3.23"'
case "$mode" in check) exit 0 ;; thinking|methods) ;; *) exit 2 ;; esac
mkdir "$out/$mode"
work="$out/$mode"
trap 'rc=$?; printf "%s\n" "$rc" > "$work/exit.tmp"; mv "$work/exit.tmp" "$work/exit.txt"' EXIT

eval_dev() {
    local model=$1 dataset=$2 dest=$3
    shift 3
    python -m tcrl.eval.run_eval --model "$model" --data "$dataset" --out "$dest" "$@"
}
eval_bfcl() {
    local model=$1 dest=$2
    shift 2
    python -m tcrl.eval.bfcl generate --data "$bfcl" --model "$model" --out "$dest.jsonl" "$@"
    # score accepts the same ID selection, but not sampling flags.
    local ids=()
    if [[ ${1:-} == --ids ]]; then ids=(--ids "$2"); fi
    "$score_python" -m tcrl.eval.bfcl score --data "$bfcl" --predictions "$dest.jsonl" --out "$dest.score.json" "${ids[@]}"
}
merge() {
    python -m tcrl.sft.train_sft --model "$1" --merge "$2" --out "$3"
}

if [[ "$mode" == thinking ]]; then
    mkdir "$work/x2" "$work/e0"
    opts=(--temperature 0.6 --top-p 0.95 --top-k 20 --max-tokens 2048 --seed 20261118)
    merge "$e0" "$p/runs/d8_x2_graded_30step/lora_adapter" "$work/x2/merged"
    for arm in e0 x2; do
        model=$e0
        if [[ "$arm" == x2 ]]; then model="$work/x2/merged"; fi
        eval_dev "$model" "$data/think_probe20.parquet" "$work/$arm/probe" "${opts[@]}" --enable-thinking
    done
    python -m tcrl.eval.d9 thinking "$work/e0/probe/summary.json" "$work/x2/probe/summary.json" --out "$work/gate.json"
    if ! python -c 'import json,sys;sys.exit(not json.load(open(sys.argv[1]))["pass"])' "$work/gate.json"; then
        rm -r -- "$work/x2/merged"
        exit 0
    fi
    for arm in e0 x2 e1 cf; do
        model=$e0
        case "$arm" in
            x2) model="$work/x2/merged" ;;
            e1) merge "$e0" "$p/runs/d4_grpo_targeted_30step/global_step_30/lora_adapter" "$work/e1/merged"; model="$work/e1/merged" ;;
            cf) merge "$e0" "$p/runs/d8_curriculum_replay/cf" "$work/cf/merged"; model="$work/cf/merged" ;;
        esac
        for style in off on; do
            flags=()
            if [[ "$style" == on ]]; then flags=(--enable-thinking); fi
            eval_dev "$model" "$data/think_dev70.parquet" "$work/$arm/$style" "${opts[@]}" "${flags[@]}"
            eval_bfcl "$model" "$work/$arm/bfcl_$style" --ids "$data/bfcl_thinking112.ids" \
                --temperature 0.6 --top-p 0.95 --top-k 20 --max-tokens 2048 "${flags[@]}"
        done
        if [[ "$arm" != e0 ]]; then rm -r -- "$model"; fi
    done
    exit 0
fi

# The common warmup is allowed once; the held-out set is never used for gates.
test -s "$out/thinking/gate.json"
bash -n scripts/autodl/d3_rl.sh
test -s /root/SDPO/verl/trainer/main_ppo.py
persist="$p/runs/$(basename "$out")/methods"
mkdir -p "$persist"
save_adapter() {
    local src=$1 arm=$2
    test -s "$src/adapter_model.safetensors"
    mkdir "$persist/$arm"
    cp -a "$src/." "$persist/$arm/"
    diff -qr "$src" "$persist/$arm"
}
base=$e0
train_sft="$data/train_sft.jsonl"
think=()
export TCRL_D9_THINK_PREFIXED=0
if ! python -c 'import json,sys;sys.exit(not json.load(open(sys.argv[1]))["pass"])' "$out/thinking/gate.json"; then
    python -m tcrl.sft.train_sft --model "$e0" --data "$data/warmup_thinking_sft.jsonl" \
        --out "$work/warmup" --enable-thinking --lr 2e-5 --max-steps 12 --batch-size 4 --grad-accum 2
    save_adapter "$work/warmup/final" warmup
    merge "$e0" "$work/warmup/final" "$work/warmed"
    base="$work/warmed"
    eval_dev "$base" "$data/think_probe20.parquet" "$work/warmup_verify" --enable-thinking \
        --temperature 0.6 --top-p 0.95 --top-k 20 --max-tokens 2048 --seed 20261118
    python -m tcrl.eval.d9 thinking "$work/warmup_verify/summary.json" --out "$work/warmup_gate.json"
    if ! python -c 'import json,sys;sys.exit(not json.load(open(sys.argv[1]))["pass"])' "$work/warmup_gate.json"; then
        exit 0
    fi
    train_sft="$data/train_thinking_sft.jsonl"
    think=(--enable-thinking)
    export TCRL_D9_THINK_PREFIXED=$(python - "$base" <<'PY'
import sys
from transformers import AutoTokenizer
t = AutoTokenizer.from_pretrained(sys.argv[1])
p = t.apply_chat_template([{"role": "user", "content": "Check."}], tokenize=False,
                          add_generation_prompt=True, enable_thinking=True)
assert not p.rstrip().endswith("</think>"), "Thinking template closed the block in the prompt"
for tag in ("<think>", "</think>", "<tool_call>", "</tool_call>"):
    ids = t.encode(tag, add_special_tokens=False)
    assert t.decode(ids, skip_special_tokens=True) == tag, f"verl decoder strips {tag}"
print(int(p.rstrip().endswith("<think>")))
PY
)
fi
opts=(--temperature 1 --top-p 1 --max-tokens 1024 --seed 20261118)
eval_dev "$base" "$data/train.parquet" "$work/train_probe" --n 8 "${opts[@]}" "${think[@]}"
eval_dev "$base" "$data/gate.parquet" "$work/gate_probe" "${opts[@]}" "${think[@]}"
python -m tcrl.eval.d9 paired --samples "$work/gate_probe/samples.jsonl" --pairs "$data/gate_pairs.jsonl" \
    --probe "$work/train_probe/samples.jsonl" --out "$work/gate.json"
if ! python -c 'import json,sys;sys.exit(not json.load(open(sys.argv[1]))["pass"])' "$work/gate.json"; then
    exit 0
fi
# Each arm schedules 22 x 32 prompt visits; RL draws eight rollouts per visit.
# The separate SFT and RL data loading schedules need not visit the same IDs.
steps=22
for arm in sft binary graded; do
    if [[ "$arm" == sft ]]; then
        python -m tcrl.sft.train_sft --model "$base" --data "$train_sft" --out "$work/sft" \
            --lr 2e-5 --max-steps "$steps" --batch-size 8 --grad-accum 4 "${think[@]}"
        save_adapter "$work/sft/final" sft
        continue
    fi
    graded=false
    if [[ "$arm" == graded ]]; then graded=true; fi
    run="d9_${arm}_$steps"
    bash scripts/autodl/d3_rl.sh grpo "$base" "$run" \
        "data.train_files=[$data/train.parquet]" "data.val_files=[$data/gate.parquet]" \
        data.seed=20261118 "data.apply_chat_template_kwargs.enable_thinking=$([[ ${#think[@]} -gt 0 ]] && echo true || echo false)" \
        data.max_response_length=1024 max_model_len=5120 actor_rollout_ref.rollout.temperature=1.0 \
        actor_rollout_ref.rollout.top_p=1.0 actor_rollout_ref.rollout.top_k=-1 \
        trainer.total_training_steps="$steps" trainer.total_epochs=2 trainer.val_before_train=False \
        trainer.test_freq=-1 trainer.save_freq="$steps" trainer.max_actor_ckpt_to_keep=1 \
        custom_reward_function.path=/root/tcrl/src/tcrl/verl_reward_d9.py custom_reward_function.name=compute_score \
        "++custom_reward_function.reward_kwargs.graded=$graded" \
        "++custom_reward_function.reward_kwargs.thinking=$([[ "${#think[@]}" -gt 0 ]] && echo true || echo false)" \
        "++custom_reward_function.reward_kwargs.prefixed=$([[ "$TCRL_D9_THINK_PREFIXED" == 1 ]] && echo true || echo false)"
    adapter="/root/autodl-tmp/runs/$run/ckpt/$run/global_step_$steps/actor/lora_adapter"
    test -s "$adapter/adapter_model.safetensors"
    mkdir "$work/$arm"
    cp -a "$adapter" "$work/$arm/final"
    diff -qr "$adapter" "$work/$arm/final"
    save_adapter "$work/$arm/final" "$arm"
    # Adapter is the evaluation artifact; release the full verified checkpoint on the session disk.
    rm -r -- "/root/autodl-tmp/runs/$run/ckpt/$run/global_step_$steps"
done
for arm in base sft binary graded; do
    model=$base
    if [[ "$arm" != base ]]; then
        model="$work/$arm/merged"
        merge "$base" "$work/$arm/final" "$model"
    fi
    dest="$work/results/$arm"
    mkdir -p "$dest"
    eval_dev "$model" "$data/heldout.parquet" "$dest/heldout" "${opts[@]}" "${think[@]}"
    python -m tcrl.eval.d9 paired --samples "$dest/heldout/samples.jsonl" --pairs "$data/heldout_pairs.jsonl" --out "$dest/paired.json"
    eval_dev "$model" "$data/external_dev541.parquet" "$dest/dev" "${opts[@]}" "${think[@]}"
    eval_bfcl "$model" "$dest/bfcl" --temperature 1 --max-tokens 1024 "${think[@]}"
    python - "$dest/bfcl.score.json" "$data/bfcl_miss78.ids" "$dest/bfcl_miss78.json" <<'PYTHON'
import json, sys
from pathlib import Path
score, ids, out = map(Path, sys.argv[1:])
wanted = set(ids.read_text().splitlines())
rows = [r for r in json.loads(score.read_text())["samples"] if r["id"] in wanted]
assert len(rows) == len(wanted) == 78
out.write_text(json.dumps({"n": len(rows), "correct": sum(r["correct"] for r in rows), "samples": rows}))
PYTHON
    if [[ "$arm" != base ]]; then rm -r -- "$model"; fi
done
