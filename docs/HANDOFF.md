# HANDOFF — 规划会话记录（2026-11-12）

## 选题是怎么定的
用户要求：与主项目互补；主体是经典后训练流水线（SFT、RL）；基于开源底座扩展；加一点 RSI。

候选与评审（Claude 调研，gpt-6-astra 与 gemini-4-argon 各自独立评审）：

| 候选 | 内容 | 结果 |
|---|---|---|
| A | Base → SFT → GRPO 做工具调用 + R-Zero 式自进化课程 | 两位评审都排第一；收窄为 A′ 后采用 |
| B | SEAL-lite（自生成微调数据写进权重 + 遗忘） | 否决：每个奖励信号是一次 LoRA 微调，两周内吞吐难控 |
| C | AZR / R-Zero 式执行验证的代码自博弈 | 否决：与副项目同领域，小预算下收益可能测不出 |
| D | 外层再套 autoresearch 式循环 | 否决：两位评审都认为会吃掉算力 |
| E | 在主项目上对成功使用笔记的轨迹做自蒸馏 | 用户有兴趣，留作以后；这次要做新项目 |

## 评审指出并已核实的问题
- Tool-R0（arXiv 2602.21320）已把 Generator–Solver 自博弈用于工具调用并分析多代课程。用户决定：思想上借鉴并如实说明，不矛盾。
- PostTrainBench v1.2 已移除 BFCL（外部审查测得约 48% 题目错误率，且有针对具体题目的刷分）。所以主评测改为自建的工具族隔离封存集。
- 奖励正确性是最大风险：校验通过不等于符合用户意图。对策见 PLAN §3.2、§11。

## 底座调研结论（2026-11-12）
- 流水线本身是通用做法，没有现成的新底座可直接套。
- 最值得借的新思想是 SDPO（ICML 2026，`lasgroup/SDPO`，基于较新 veRL，开源）：工具调用的报错正好是它需要的文字反馈。README 里没有工具调用任务，LoRA 支持未确认，放在 D1 验证。
- Tool-R0 仓库只有标题，没有可用代码。Synthesize-and-Reward（IBM）论文写代码「即将发布」，未核实到。
- ToolRL（`qiancheng0/ToolRL`）基于 veRL，有数据和训练脚本，可参考奖励实现。

## D1 桌面调研更正（2026-11-12，详见 `docs/notes/d1_base_survey.md`）
- SDPO 仓库自带工具调用任务（ToolAlpaca）和 GRPO 基线，版本 veRL 0.7.0.dev，有 LoRA 配置。上面「README 里没有工具调用任务」的说法作废。
- Tool-R0 仓库有完整流程代码（trl + peft，非 veRL）。上面「没有可用代码」的说法作废。
- LoRA + EMA 老师预计可直接用（ref 也挂 LoRA；主控最初读错，astra 纠正）；工具任务后期崩溃有作者给的对策（JS 损失）。
- Qwen3-4B-Base 与 Toucan（SFT config 11.9 万条，Apache-2.0）已核实存在。

## 未核实的说法（引用前先查）
- Tool-R0 的发表会议、ToolRL / Tool-N1 的具体覆盖范围（来自 Gemini 评审，未逐条核对）。
- Qwen3-4B-Base 在 HF 上的确切 repo 名与 revision（R-Zero README 列有 `Qwen/Qwen3-4B-Base`）。
- Toucan 的许可与单轮子集规模。
- SDPO 是否已有人用于工具调用。

## 待办
1. 用户确认 CLAUDE.md「待确认」各项。
2. `git init`、建 GitHub 仓库、加 `third_party/` submodule。
3. D1：拉起底座并做 Go/No-Go ①。
