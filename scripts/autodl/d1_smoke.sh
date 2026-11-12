#!/bin/bash
# usage: smoke.sh <grpo|sdpo> <model_path> <tag> [extra hydra args]
export PATH=/root/miniconda3/bin:$PATH
MODE=$1; MODEL=$2; TAG=$3; shift 3
cd /root/SDPO
export PYTHONPATH=/root/SDPO USER=root WANDB_MODE=disabled
export TASK=datasets/tooluse EXPERIMENT=$TAG
unset VLLM_ATTENTION_BACKEND; export VLLM_USE_V1=1 PYTHONUNBUFFERED=1
OUT=/root/autodl-tmp/smoke/$TAG; mkdir -p $OUT
COMMON="vars.dir=/root/SDPO vars.log_dir=$OUT vars.ckpt_dir=$OUT/ckpt \
custom_reward_function.path=/root/SDPO/verl/utils/reward_score/feedback/__init__.py \
trainer.logger=[console] trainer.n_gpus_per_node=1 trainer.total_training_steps=10 trainer.test_freq=5 trainer.save_freq=0 \
actor_rollout_ref.model.path=$MODEL critic.model.path=$MODEL \
actor_rollout_ref.model.lora_rank=32 actor_rollout_ref.model.lora_alpha=32 \
data.train_batch_size=8 data.max_response_length=1024 \
actor_rollout_ref.rollout.n=4 actor_rollout_ref.rollout.val_kwargs.n=2 \
actor_rollout_ref.rollout.gpu_memory_utilization=0.30 actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
actor_rollout_ref.actor.optim.lr=1e-5 actor_rollout_ref.actor.optim.lr_warmup_steps=0 \
algorithm.rollout_correction.rollout_is=token"
if [ "$MODE" = grpo ]; then
  CFG=baseline_grpo; EXTRA="max_model_len=4096 actor_rollout_ref.actor.ppo_mini_batch_size=8"
else
  CFG=sdpo; EXTRA="max_model_len=8192 actor_rollout_ref.actor.ppo_mini_batch_size=8 \
actor_rollout_ref.actor.self_distillation.max_reprompt_len=4096 \
actor_rollout_ref.actor.self_distillation.distillation_topk=100 \
actor_rollout_ref.actor.self_distillation.alpha=0.5 \
actor_rollout_ref.actor.self_distillation.teacher_update_rate=0.05 \
actor_rollout_ref.actor.self_distillation.dont_reprompt_on_self_success=True"
fi
( nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -l 5 > $OUT/gpu_mem.txt ) & MON=$!
python -m verl.trainer.main_ppo --config-name $CFG $COMMON $EXTRA "$@" > $OUT/train.log 2>&1
RC=$?; kill $MON; echo "RC=$RC" >> $OUT/train.log
echo "RC=$RC peak_mem_MiB=$(sort -n $OUT/gpu_mem.txt | tail -1)" > $OUT/done.txt
