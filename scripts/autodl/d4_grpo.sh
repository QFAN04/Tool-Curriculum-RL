#!/bin/bash
set -euo pipefail
cd /root/tcrl

persist=/root/autodl-fs/tcrl
model=$persist/models/d3_sft_gold_disjoint_merged
pool=$persist/data/processed/gold_disjoint/export/rl_targeted.parquet
dev=/root/tcrl/data/processed/export/dev.parquet
run=${1:-d4_grpo_targeted_30step}
out=/root/autodl-tmp/runs/$run
if (( $# )); then shift; fi

test -s "$model/model.safetensors.index.json"
sha256sum --check <<EOF
0f9c0404f6d557dabd0c79f5a0369b311f1dd4e51502b6063c5255233f3f8119  $pool
0d862fe5e4c5203cbea1963e47cf90d8cd121b937d870f1c489ce0383f27bc1c  $dev
EOF

exec bash scripts/autodl/d3_rl.sh grpo "$model" "$run" \
    "data.train_files=[$pool]" "data.val_files=[$dev]" \
    data.seed=20261114 actor_rollout_ref.rollout.temperature=1.0 \
    actor_rollout_ref.rollout.top_p=1.0 actor_rollout_ref.rollout.top_k=-1 \
    trainer.total_training_steps=30 trainer.val_before_train=True \
    trainer.test_freq=10 trainer.save_freq=10 trainer.max_actor_ckpt_to_keep=2 \
    "trainer.rollout_data_dir=$out/rollouts" "trainer.validation_data_dir=$out/validation" "$@"
