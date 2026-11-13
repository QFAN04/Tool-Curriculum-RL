#!/bin/bash
# 用法：d3_rl.sh <grpo|sdpo> <model_path> <run_id> [额外 hydra 参数]
# 环境变量 TCRL_FEEDBACK_GOLD=1/0 控制 SDPO 反馈里是否含标准答案（见 src/tcrl/verl_reward.py）
export PATH=/root/miniconda3/bin:$PATH
MODE=$1; MODEL=$2; RUN=$3; shift 3
cd /root/SDPO
export PYTHONPATH=/root/SDPO:/root/tcrl/src USER=root WANDB_MODE=disabled TASK=tcrl EXPERIMENT=$RUN
unset VLLM_ATTENTION_BACKEND; export VLLM_USE_V1=1 PYTHONUNBUFFERED=1
OUT=/root/autodl-tmp/runs/$RUN; mkdir -p $OUT
D=/root/tcrl/data/processed/export
COMMON="vars.dir=/root/SDPO vars.log_dir=$OUT vars.ckpt_dir=$OUT/ckpt \
data.train_files=[$D/rl.parquet] data.val_files=[$D/dev.parquet] \
data.max_prompt_length=4096 data.max_response_length=512 data.filter_overlong_prompts=True \
custom_reward_function.path=/root/tcrl/src/tcrl/verl_reward.py custom_reward_function.name=compute_score \
trainer.logger=[console] trainer.n_gpus_per_node=1 trainer.save_freq=50 \
actor_rollout_ref.model.path=$MODEL critic.model.path=$MODEL \
actor_rollout_ref.model.lora_rank=32 actor_rollout_ref.model.lora_alpha=32 \
actor_rollout_ref.rollout.tensor_model_parallel_size=1 actor_rollout_ref.rollout.gpu_memory_utilization=0.35 \
actor_rollout_ref.rollout.n=8 data.train_batch_size=32 actor_rollout_ref.actor.ppo_mini_batch_size=32 \
actor_rollout_ref.rollout.val_kwargs.n=1 actor_rollout_ref.rollout.val_kwargs.temperature=0 actor_rollout_ref.rollout.val_kwargs.do_sample=False \
actor_rollout_ref.actor.use_dynamic_bsz=True actor_rollout_ref.actor.ppo_max_token_len_per_gpu=24576 \
actor_rollout_ref.actor.optim.lr=1e-5 actor_rollout_ref.actor.optim.lr_warmup_steps=0 \
algorithm.rollout_correction.rollout_is=token"
if [ "$MODE" = grpo ]; then
  CFG=baseline_grpo; EXTRA="max_model_len=4608"
else
  CFG=sdpo; EXTRA="max_model_len=6656 actor_rollout_ref.actor.self_distillation.max_reprompt_len=6144 \
actor_rollout_ref.actor.self_distillation.distillation_topk=100 \
actor_rollout_ref.actor.self_distillation.alpha=0.5 \
actor_rollout_ref.actor.self_distillation.teacher_update_rate=0.05 \
actor_rollout_ref.actor.self_distillation.dont_reprompt_on_self_success=True"
  if [ "${TCRL_FEEDBACK_GOLD:-1}" = "0" ]; then
    EXTRA="$EXTRA actor_rollout_ref.actor.self_distillation.success_reward_threshold=2.0"
  fi
fi
( nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -l 5 > $OUT/gpu_mem.txt ) & MON=$!
python -m verl.trainer.main_ppo --config-name $CFG $COMMON $EXTRA "$@" > $OUT/train.log 2>&1
RC=$?; kill $MON; echo "RC=$RC" >> $OUT/train.log
echo "RC=$RC peak_mem_MiB=$(sort -n $OUT/gpu_mem.txt | tail -1)" > $OUT/done.txt
exit "$RC"
