# 对策审查请求（只读审查；不要修改任何文件，不要运行训练）

你是独立审查人。请**批判性**地检查下面每条「对策」是否正确、是否对症，不要附和。输出中文，1200 字以内。能读文件的话，仓库在当前目录：计划 `docs/PLAN.md`，调研 `docs/notes/d1_base_survey.md`，上游源码 `third_party/SDPO/`（veRL 0.7.0.dev 的分支）。

## 项目一句话
Qwen3-4B-Base → LoRA SFT → RL（GRPO 对照 SDPO 自蒸馏）做单轮工具调用；再做最多两轮「失败驱动的自生成课程」，与静态数据续训、无引导生成做等成本三臂对照。评测用按工具族隔离的自建封存集。单卡 RTX PRO 6000 96GB，只做 LoRA，约 14 个工作日。

## 需要你判断的对策

**C1 LoRA 下的 SDPO 老师。** 主控读源码的结论（未运行验证）：
- SDPO 模式下 actor worker 角色为 `actor_rollout_ref`，另建一份完整模型作为老师：`third_party/SDPO/verl/workers/fsdp_workers.py` 约 852–905 行，`self.actor.teacher_module = self.ref_module_fsdp`。
- EMA 更新在 `verl/workers/actor/dp_actor.py` 的 `_update_teacher`：
  ```python
  for teacher_param, student_param in zip(self.teacher_module.parameters(), self.actor_module.parameters()):
      teacher_param.data.mul_(1.0 - update_rate).add_(student_param.data, alpha=update_rate)
  ```
  actor 套 peft 后参数序列里多了 lora_A / lora_B，与老师（无 LoRA）的序列错位。
- 备选：(a) `teacher_update_rate=0`，老师固定为初始模型；(b) 写 patch，老师也挂 LoRA，只对 adapter 权重按名字做 EMA；(c) 用仓库已有的 `teacher_regularization="trust-region"`（`TrustRegionTeacher`：老师 logits = lerp(ref_logits, student_logits, mix_coef)），不改代码。
- 主控倾向：先 (a) 冒烟，正式实验用 (b)。
- 请判断：源码结论对不对？(a)(b)(c) 哪个最合适？(c) 在 LoRA 下是否真的可用（`student_module` 是带 adapter 的 actor）？固定老师是否会让 SDPO 退化成别的东西？

**C2 SDPO 在工具任务后期崩溃**（上游 issue #25：Qwen3-8B 默认配置约 400 步后准确率掉到接近 0；作者回复影响最大的是老师 EMA 速率和损失类型，Jensen-Shannon 比正 / 反向 KL 稳定）。对策：`alpha=0.5`（JS），按 dev 选 checkpoint，训练步数控制在 150–200 步。是否对症？还有什么更可能的崩溃原因需要先排除（比如全对后没有失败样本、top-k 蒸馏、IS 截断）？

**C3 小模型上 SDPO 可能只是记忆**（上游 issue #26：多人在 1.7B–4B 数学任务上看到训练奖励涨、验证不涨；也有人用 8B 得到正结果）。对策：不回避，把它作为研究问题，在「未见工具族」上比较 GRPO 与 SDPO。这样的对照能否干净地区分「记忆」与「学会」？需要加什么对照或指标？

**C4 反馈是否包含标准答案。** 上游工具打分器的反馈形如 `Actions mismatch: predicted [...], expected [...]`，即老师能看到标准答案；另外 SDPO 还会把同组里成功的 rollout 作为演示给老师。对策：主设置沿用上游（含答案），「只给校验错误、不给答案」作为小消融。含答案时，SDPO 与「直接在标准答案上做 SFT」还有什么本质区别？GRPO 对照 SDPO 是否因此不公平（SDPO 用了标签的 token 级信息，GRPO 只用了 0/1）？该怎么表述或加什么基线？

**C5 奖励。** 主奖励为归一化后调用集合与标准答案完全一致记 1，否则 0；只有 probe 显示混合组太少时才换分级奖励。上游打分器把多个调用的参数合并成一个 dict 比较，我们改为逐调用比对。二值奖励在 SFT 之后是否太稀疏或太饱和？

**C6 自生成课程的伪标签质量**（R-Zero issue #20：有人复现时伪标签准确率仅 30–40%）。对策：出题时先写标准调用再写问题；过滤 = schema 合法 → 标准调用通过校验 → 模型多次作答一致性达阈值 → 去重 → 不含 dev / test 工具族；每轮人工抽查 100 条。「模型多次作答一致」这个过滤是否会系统性筛掉模型不会的题，从而与「失败驱动」的目的相抵？Tool-R0 用的是 k=10、阈值 0.20 的一致性加难度配比（易 / 中 / 难 = 0.2 / 0.5 / 0.3），我们该怎么设？

**C7 出题失败率**（R-Zero issue #27：一半以上题目不合格，原因是 thinking 占满 max_tokens）。对策：出题时关 thinking，统计各过滤环节通过率。我们的模型是 Base 经 SFT 得到的，只学过「答题」没学过「出题」，它能出题吗？是否需要在 SFT 数据里加少量出题样本，或用 few-shot？这会不会破坏三臂对照的公平性？

**C8 等成本三臂对照**（S 静态续训 / U 无引导生成 / F 失败驱动生成；训练步数相同，生成与过滤的 GPU 时间计入成本）。这个设计能否把「更多数据」「自生成」「失败驱动」三个因素分开？S 臂在成本上吃亏还是占便宜？

**C9 SFT → RL 的衔接。** 计划：SFT 得到 LoRA，合并进基座得到完整权重，再在其上挂新的 LoRA 做 RL；SDPO 的老师初始为合并后的 SFT 模型。有没有坑（合并精度、bf16、SFT 量太大导致 RL 无梯度）？

## 请按此格式回答
对每条 C1–C9：**结论（正确 / 部分正确 / 有问题）**＋一两句理由＋如果要改，具体怎么改。最后列出你认为计划里**最可能导致项目失败的前三个问题**，以及任何主控没有想到的风险。
