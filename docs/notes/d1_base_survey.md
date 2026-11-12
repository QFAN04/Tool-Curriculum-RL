# D1 桌面调研：底座源码与各参考项目的 issue / PR（2026-11-12）

> 范围：只读源码和 issue，没有上 GPU。Go/No-Go ① 的「能否在单卡上以 LoRA 跑通」仍需 GPU 冒烟确认。
> GitHub API 未登录有频率限制，ToolRL / R-Zero 的 issue 只细读了下面列出的几条，其余只看了标题。

## 1. SDPO（`lasgroup/SDPO`，submodule 固定在 `7c457fc`，Apache-2.0）

### 对我们有利的事实
- **版本是 veRL `0.7.0.dev`**，与主项目用的 veRL 0.7.0 同代，已有的镜像经验和检查脚本大概率能沿用。
- **原论文本身就有工具调用任务**：仓库自带 `datasets/tooluse/`（ToolAlpaca，train 4046 条、test 68 条，JSONL）和打分器 `verl/utils/reward_score/feedback/tooluse.py`。`run_local_sdpo.sh` 默认数据就是它，且默认 `N_GPUS_PER_NODE=1`。
- 同一仓库有 GRPO 基线：`run_local_grpo.sh`、`verl/trainer/config/baseline_grpo.yaml`。两条 RL 臂共用一套代码。
- 配置里有 LoRA（`actor_rollout_ref.model.lora_rank` / `lora_alpha` / `lora_adapter_path`），也有 SFT trainer（`sft_trainer.yaml`）。
- issue #29：有人贴了 RTX 6000 Blackwell（即 PRO 6000 同代）的完整环境版本表，说明需要设 bfloat16；`requirements.txt` 注明 Blackwell 需 vllm ≥ 0.12 且「未充分测试」。

### 需要处理的问题
1. **LoRA + EMA 老师不能直接用（读源码得出，未运行验证）。**
   - SDPO 模式下 actor worker 的角色是 `actor_rollout_ref`，会再建一份完整模型作为老师（`fsdp_workers.py` 第 852–905 行）。
   - EMA 更新是 `zip(teacher.parameters(), actor.parameters())` 逐个对齐（`dp_actor.py` 第 132–151 行）。actor 套了 peft 之后参数序列里多了 `lora_A / lora_B`，与老师的序列对不上。
   - 可选做法：(a) `teacher_update_rate=0`，老师固定为初始模型，不需要改代码，但老师不会进步；(b) 写 patch，让老师也挂一份 LoRA，只对 adapter 权重按名字做 EMA。倾向先用 (a) 冒烟，再做 (b)。(b) 是一处有价值的自研改动。
2. **工具任务上训练后期会崩**（issue #25）：Qwen3-8B 默认配置约 400 步后准确率掉到接近 0。作者回复：影响最大的是老师 EMA 速率和损失类型，Jensen-Shannon（`alpha=0.5`）比正向 / 反向 KL 稳定得多。对策：用 `alpha=0.5`，按 dev 选 checkpoint，步数不超过出现崩溃的区间。
3. **小模型上 SDPO 的泛化有争议**（issue #26）：多人反馈 1.7B–4B 在数学上训练奖励上升但验证集不涨，怀疑是在记忆演示；也有人用 Qwen3-8B 得到正结果。这正好是我们 RQ2 要在「未见工具族」上检验的问题，报告里可以直接回应。
4. **自带打分器的反馈会把标准答案写进去**（`expected {gt_actions}`）。我们的 PLAN §3.3 原先写「不泄漏答案」。需要定：沿用上游做法（老师能看到答案），还是只给校验错误信息。建议两种都留，作为 SDPO 臂内的一个小消融，主设置先沿用上游。
5. **自带打分器较粗**：把多个调用的参数合并成一个 dict 再比较，区分不了「参数给错了调用」。我们自己的 `reward.py` 要按调用逐个比对。
6. ToolAlpaca 测试集只有 68 条，不够做配对检验，这是我们要自建按工具族隔离评测集的理由之一（issue #333 也有人问清洗规则，无回复）。

## 2. Tool-R0（`emrecanacikgoz/Tool-R0`）

**更正：之前说「没有可用代码」是错的。** README 只有标题，但仓库里有完整流程代码（约 4200 行）：
- `step1_generator.py`（训练出题者）→ `step2_gen.py`（生成）→ `step2_genverify.py`（求解者 k 次作答一致性校验，`k_verify=10`、`tau_verify=0.20`）→ `step2_select_curriculum.py`（按 easy / medium / hard = 0.2 / 0.5 / 0.3 配比选 2000 条）→ `step3_solver.py`（训练求解者）。
- 技术栈是 `trl` + `peft` + `vllm==0.8.4` + deepspeed，**不是 veRL**；默认 Qwen2.5-1.5B-Instruct，5 轮，每轮出题者和求解者各 50 步，脚本按 3–4 卡写。
- `rewards_generator.py` 里有可以直接参考的函数：schema 校验、调用归一化、求解者一致性、难度带通奖励、重复惩罚、占位符问题过滤。`rewards_solver.py` 有数值宽松匹配和按调用打分。
- issue 只有 2 条（问 HF 发布和后续发布计划），没有复现讨论。

用法：不作为底座，**过滤和归一化的实现可以对照着写**，课程配比作为我们 F 臂的参考默认值。

## 3. ToolRL（`qiancheng0/ToolRL`）
- issue #41：仓库依赖的 vllm / transformers 太旧，不支持 Qwen3。作者建议用新版 veRL，只把 `verl/utils/reward_score/rlla.py` 搬过去。→ 我们不拿它当底座，只参考这个奖励文件。
- issue #52（无人回复）：有人指出 `use_kl_loss=True` 时 KL 项符号反了导致训练崩溃。我们不用它的训练代码；SDPO 里也不能同时开 KL（`main_ppo.py` 第 132 行会报错）。
- issue #14：数据 `rlla_rl.json` 有转换错误和参数类型错误，作者承认。→ 不用它的数据，也印证了 D2 人工核查 100 条的必要。
- 标题里还有 SFT 训练（#19、#42）、与 BFCL 评测对接（#36）、checkpoint 合并（#34、#11）等，用到时再读。

## 4. R-Zero（`Chengsong-Huang/R-Zero`）
- issue #27：用 Qwen3-8B 出题，超过一半得分为 −1（不符合输出模板）；提问者后来发现是 thinking 内容占满了 `max_tokens`。→ 我们出题时关 thinking 或放大 token 上限，并统计过滤各环节的通过率。
- issue #20：有人复现第 1 轮，筛选后伪标签准确率只有约 30–40%，低于论文；作者表示判定脚本不在手边，判定方式有差别。→ 伪标签质量是这类方法的真实软肋，我们每轮人工抽查 100 条是必要的，且这本身是 RQ4 的看点。
- issue #18「为什么用 Base 而不是 Instruct」、#8「reward hacking」只看了标题，写报告时再读。

## 5. 模型与数据
- `Qwen/Qwen3-4B-Base`：存在，Apache-2.0，4.02B 参数。
- `Agent-Ark/Toucan-1.5M`：Apache-2.0；四个 config——Kimi-K2 51.9 万、OSS 45.7 万、Qwen3 55.2 万、**SFT 11.9 万条（425 MB，6 列）**。SFT 这个 config 体量合适，优先看它。字段格式还没看到（数据预览接口限流），D2 下载后确认；轨迹多为多步多轮，需要筛出单轮 / 并行调用的样本，筛不够就退回 xlam-60k。

## 6. 对 Go/No-Go ① 的桌面结论
- 底座选 SDPO 仓库：**倾向 Go**。理由：版本同代、自带工具任务和 GRPO 基线、有 LoRA 配置、有 PRO 6000 同代卡的安装记录。
- 还没验证、必须上 GPU 才能确认的：
  1. 在 PRO 6000 上装好环境（按 #29 的版本表）；
  2. 用小模型在自带 ToolAlpaca 上跑 GRPO 10 步、SDPO（`teacher_update_rate=0`，LoRA）10 步，无报错、轨迹可读；
  3. 4B + LoRA + SDPO 的峰值显存（老师是另一份完整模型，加上全词表或 top-k logits 蒸馏）。
- 冒烟的预计花费到时按当时单价估，超过 ¥20 先问用户。
