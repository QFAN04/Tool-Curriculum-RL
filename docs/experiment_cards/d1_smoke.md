# D1 冒烟：SDPO 仓库在单卡 PRO 6000 上的 GRPO / SDPO（LoRA）

- 日期：2026-11-12
- 目的：Go/No-Go ①——SDPO 仓库能否在单卡 RTX PRO 6000 96GB 上以 LoRA 跑通 GRPO 与 SDPO。
- 硬件：AutoDL 西北B，RTX PRO 6000 Blackwell 96GB × 1，驱动 580.82.09；单价 ¥8.36/h（用户开机时报的价格）。
- 环境：私有镜像 `ssrl-verl070-vllm012`（Python 3.12.3、torch 2.9.0+cu129、vLLM 0.12.0、transformers 4.57.3、peft 0.18.0、ray 2.52.1）。没有重装依赖，SDPO 通过 `PYTHONPATH=/root/SDPO` 覆盖镜像里的 verl 0.7.0。
- 代码：`third_party/SDPO` @ `7c457fc`，无改动。启动脚本 `scripts/autodl/d1_smoke.sh`。
- 数据：仓库自带 ToolAlpaca（`datasets/tooluse`，train 4046 / test 68），用 `data/preprocess.py` 转 parquet。
- 模型：Qwen3-0.6B（官方 Instruct 版，仅用于冒烟）。

## 环境上遇到并解决的问题
1. 私有镜像恢复后 site-packages 里有残留旧文件（numpy 等 321 个），导致 scipy / pandas / cryptography 导入报错。用主项目的 `scripts/autodl/clean_stale_site_packages.py` 清理后正常。**每次从该镜像开新实例都要先跑一遍。**
2. 另装了 `math-verify[antlr4_9_3]==0.8.0`、`latex2sympy2_extended==1.10.2`、`word2number==1.1`、`pylatexenc==2.10`（上游打分器模块导入时需要）。
3. 上游配置默认 `tensor_model_parallel_size=2`，单卡须设为 1。
4. `user.yaml` 里的路径写死为作者集群，用命令行覆盖 `vars.dir / vars.log_dir / vars.ckpt_dir / custom_reward_function.path`。

## 配置（两组相同部分）
LoRA r32 / α32 / all-linear，lr 1e-5，`train_batch_size=8`，`rollout.n=4`，`max_response_length=1024`，10 步，第 5、10 步在 test 68 条上评测（每题采样 2 次），`rollout_is=token`。
- GRPO：`baseline_grpo`，`max_model_len=4096`。
- SDPO：`sdpo`，`alpha=0.5`（JS）、`teacher_update_rate=0.05`（EMA）、`distillation_topk=100`、`max_reprompt_len=4096`、`dont_reprompt_on_self_success=True`、`max_model_len=8192`。反馈为上游默认（含标准答案）。

## 结果

| run_id | 退出码 | 每步耗时 | 训推概率差均值（第 10 步附近） | nvidia-smi 峰值 | test acc mean@2（第 5 / 10 步） |
|---|---|---|---|---|---|
| `d1_smoke_grpo_q3-0p6b_lora` | 0 | 约 36 s | 0.004–0.005 | 29.8 GB | 0.456 / 0.485 |
| `d1_smoke_sdpo_q3-0p6b_lora` | 0 | 约 51 s | 0.005 | 36.2 GB | 0.478 / 0.463 |

- 10 步、每步 32 条轨迹，test acc 的变化在噪声范围内，**不作任何效果解读**；这里只说明流程跑通、评测能出数。
- 显存峰值里约 29 GB 是 vLLM 按 `gpu_memory_utilization=0.30` 预占的，不代表 0.6B 的真实需求。
- **LoRA + EMA 老师直接可用**：`teacher_update_rate=0.05` 下 10 步无报错，`self_distillation/*` 指标正常输出（例如第 10 步 `reprompt_sample_fraction=1.0`、`success_sample_fraction=0.375`）。印证了 astra 审查的结论，不需要 patch。
- 未验证：老师与学生的 `lora_A` 初始化是否相同；恢复训练；超过 10 步后的稳定性。

## 4B + LoRA + SDPO 显存与耗时（run_id `d1_mem_sdpo_q3-4b-base_lora`）
- 模型 Qwen3-4B-Base（未经 SFT），其余同上，但 `train_batch_size=32`、`rollout.n=8`（每步 256 条轨迹）、`ppo_mini_batch_size=32`。跑完第 1 步后手动停止（只为测显存和耗时）。
- 第 1 步日志：
  - `perf/max_memory_allocated_gb` 68.8，`max_memory_reserved_gb` 68.9；nvidia-smi 采样峰值 63.0 GB（5 秒采样一次，可能漏掉瞬时峰值）。**96 GB 单卡放得下，余量约 27 GB。**
  - 单步 934 s：生成 147 s，旧 logprob 60 s，更新 726 s。
  - `response_length/mean` 497，`clip_ratio` 0.37（37% 的输出顶到 1024 token 上限）；`critic/score/mean` 0.016。
- 解读：这是**最坏情况**。Base 模型没学过格式，输出又长又不停。按这个速度 150 步约需 39 小时，不可接受；但 SFT 之后输出应当短得多，更新阶段也可以调大 micro-batch（现在是 1）。**真实吞吐必须在 D3 的 SFT 之后重测**，再据此定每组步数和预算。
- 4B-Base 在 ToolAlpaca 上几乎全错（0.016），说明不经 SFT 直接做 RL 没有可学信号，SFT 这一步是必须的。

## 花费
实例运行约 1 小时（约 23:00–00:00），按 ¥8.36/h 估约 ¥8–9，以账单为准。

## 留存
- 实例已关机（2026-11-13 00:00 左右，在实例内执行 shutdown）。
- 文件存储 `/root/autodl-fs/tcrl/`（西北B）：`models/Qwen3-4B-Base`（7.6 GB）、三个 run 的日志、`smoke.sh`、`clean_stale.py`。
- 本地 `runs/d1_*`：三个 run 的 `train.log` 与 `gpu_mem.txt`。
- 实例系统盘上有 `/root/SDPO`（`7c457fc`）和新装的 4 个包、清理过的 site-packages。**尚未另存镜像**；不存的话下次要重做清理和安装（约 5 分钟）。

## 结论
Go/No-Go ①：**Go**。底座采用 `lasgroup/SDPO`；单卡 PRO 6000 上 4B + LoRA + SDPO 显存够用。遗留问题是吞吐，D3 重测。
