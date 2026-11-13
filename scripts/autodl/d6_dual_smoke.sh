#!/bin/bash
set -euo pipefail
cd /root/tcrl
persist=/root/autodl-fs/tcrl
model=$persist/models/d3_sft_gold_disjoint_merged
pool=$persist/data/processed/gold_disjoint/export/rl_targeted.parquet
dev=/root/tcrl/data/processed/export/dev.parquet
root=/root/autodl-tmp/runs
status=$root/d6_dual_smoke
mkdir -p "$status"
trap 'rc=$?; printf "%s\n" "$rc" > "$status/exit.tmp"; mv "$status/exit.tmp" "$status/exit.txt"' EXIT
sha256sum --check <<EOF
0f9c0404f6d557dabd0c79f5a0369b311f1dd4e51502b6063c5255233f3f8119  $pool
0d862fe5e4c5203cbea1963e47cf90d8cd121b937d870f1c489ce0383f27bc1c  $dev
EOF
nvidia-smi topo -m > "$status/topology.txt"

for mode in grpo sdpo; do
    run=d6_dual_${mode}_5step
    printf '%s\n' "$run" > "$status/phase.txt"
    TCRL_FEEDBACK_GOLD=0 bash scripts/autodl/d3_rl.sh "$mode" "$model" "$run" \
        "data.train_files=[$pool]" "data.val_files=[$dev]" \
        data.seed=20261114 trainer.n_gpus_per_node=2 \
        actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
        actor_rollout_ref.rollout.temperature=1.0 actor_rollout_ref.rollout.top_p=1.0 \
        actor_rollout_ref.rollout.top_k=-1 trainer.total_training_steps=5 \
        trainer.val_before_train=False trainer.test_freq=-1 trainer.save_freq=-1 \
        trainer.resume_mode=disable "trainer.rollout_data_dir=$root/$run/rollouts"
done
