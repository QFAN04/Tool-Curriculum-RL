#!/bin/bash
set -euo pipefail
cd /root/tcrl
export PATH=/root/miniconda3/bin:$PATH PYTHONPATH=/root/tcrl/src
p=/root/autodl-fs/tcrl
r=/root/autodl-tmp/runs
mode=${1:-preflight}
case "$mode" in preflight|formal|gold_only) ;; *) exit 2 ;; esac
status=$r/d6_single_$mode
model=$p/models/d3_sft_gold_disjoint_merged
pool=$p/data/processed/gold_disjoint/export/rl_targeted.parquet
dev=data/processed/export/dev.parquet
mkdir -p "$status"
trap 'rc=$?; printf "%s\n" "$rc" > "$status/exit.tmp"; mv "$status/exit.tmp" "$status/exit.txt"' EXIT
sha256sum -c <<EOF
0f9c0404f6d557dabd0c79f5a0369b311f1dd4e51502b6063c5255233f3f8119  $pool
0d862fe5e4c5203cbea1963e47cf90d8cd121b937d870f1c489ce0383f27bc1c  $dev
EOF

train() {
    local run=$1 gold=$2 steps=$3
    shift 3
    printf '%s\n' "$run" > "$status/phase.txt"
    TCRL_FEEDBACK_GOLD=$gold bash scripts/autodl/d3_rl.sh sdpo "$model" "$run" \
        "data.train_files=[$pool]" "data.val_files=[/root/tcrl/$dev]" \
        data.seed=20261114 actor_rollout_ref.rollout.temperature=1.0 \
        actor_rollout_ref.rollout.top_p=1.0 actor_rollout_ref.rollout.top_k=-1 \
        actor_rollout_ref.rollout.gpu_memory_utilization=0.25 \
        actor_rollout_ref.actor.ppo_max_token_len_per_gpu=16384 \
        "trainer.total_training_steps=$steps" trainer.resume_mode=disable \
        "trainer.rollout_data_dir=$r/$run/rollouts" "$@"
}

if [ "$mode" = preflight ]; then
    train d6_single_e2b_memory_check 1 5 trainer.val_before_train=True trainer.test_freq=-1 trainer.save_freq=-1
    exit 0
fi

run_arm() {
    local run=$1 gold=$2
    shift 2
    train "$run" "$gold" 30 trainer.val_before_train=True trainer.test_freq=10 \
        trainer.save_freq=30 trainer.max_actor_ckpt_to_keep=1 \
        "trainer.validation_data_dir=$r/$run/validation" "$@"
    adapter=$r/$run/ckpt/$run/global_step_30/actor/lora_adapter
    dest=$p/runs/$run
    mkdir -p "$dest"
    cp -a "$adapter" "$dest/"
    cmp "$adapter/adapter_model.safetensors" "$dest/lora_adapter/adapter_model.safetensors"
    merged=/root/autodl-tmp/models/$run
    python -m tcrl.sft.train_sft --model "$model" --merge "$adapter" --out "$merged"
    python -m tcrl.eval.run_eval --model "$merged" --data "$dev" --out "$r/$run/independent_dev"
    # The verified adapter reproduces the model. Release the merged copy before the next arm.
    rm -r -- "$merged"
}

if [ "$mode" = gold_only ]; then
    run_arm d6_single_gold_only_30step 1 actor_rollout_ref.actor.self_distillation.success_reward_threshold=2.0
else
    run_arm d6_single_e2a_30step 0
    run_arm d6_single_e2b_30step 1
fi
