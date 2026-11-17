# CLAUDE.md — tool-curriculum-rl

把 `Qwen3-4B-Base` 经 LoRA SFT → GRPO / SDPO 训练成工具调用模型，并检验：在等生成与训练预算下，由模型自己的失败驱动的自生成课程（`C-F`），能否比均匀自生成（`C-U`）或静态复习（`C-S`）更好地泛化到未见工具域。

- **项目状态（2026-11-15 已收官）：** D1–D12 实验与封存终评全部完成，研究总报告见 `docs/FINAL_REPORT.md`，分阶段进度见 `docs/PROGRESS.md`，完整计划见 `docs/PLAN.md`。
- 核心结论索引（均来自对应 run 真实日志与归档）：
  - **SFT（D3 `d3_sft_q3-4b-base_lora_gold_disjoint` → `E0`）**：修复共享词表中 6 个特殊 token 的塌缩向量并放开训练后，D12 封存集（`d12-final-34960`）未见工具族严格 greedy 从 `B0`（Base 3-shot）的 `276/1842`（`14.98%`）提升至 `1736/1842`（`94.25%`），超过官方 `Qwen3-4B-Instruct`（`B1`: `1684/1842`）；`E0` 同期在 D9 BFCL 得 `1056/1240`（D12 当轮 `1058/1240`）。
  - **原分布封存终评（D12 `d12-final-34960`）**：`D9 Binary GRPO` 拿到全量严格 greedy 最高分 `2890/3018`（`95.76%`，未见族 `1741/1842`，BFCL `1072/1240`）；`E2b-gold-only` 与 `D8 C-F` 并列未见族 `pass@8` 最高（`1783/1842` `96.80%` 严格 / `1786/1842` `96.96%` 默认值等价）。Holm 校正后，SFT 之后的封存集增益未达统计显著。
  - **隐式换算留出集（D9 `d9-methods-43482` & D10 `d10-curriculum-41786`）**：同一批 392 对题需要隐式换算时，`E0` 从直给 `392/392` 降至 `241/392`（D10 同配置复评 `240/392`）；D9 `Targeted SFT` 达 `351/392`（`89.54%`），`Binary GRPO` 达 `336/392`（`85.71%`），`Graded GRPO` 为 `295/392`；D10 `C-F` 达 `337/392`（`85.97%`），在等预算下较 `C-U`（`305/392`）净增 `32` 题（`+8.16 pp`，Holm `p=4.66e-10`）。

## 已定技术方案
- 选题与方法：工具调用后训练流水线（Base → SFT → RL）+ 反馈/失败驱动的迭代课程；思想借鉴 Tool-R0，对外如实说明。
- 评测体系：主评测为按工具族隔离的自建封存集（D12，3,018 题）；隐式换算迁移用 4 个未见域的 392 对 Literal / Conversion 留出集（D9/D10）；外部迁移用 BFCL 单轮 Python 切片（1,240 题）。
- 底座与算力：`lasgroup/SDPO`（veRL 0.7.0.dev，含 GRPO 与 SDPO），submodule 固定在 `7c457fc`；模型为 `Qwen3-4B-Base` + LoRA，单卡 RTX PRO 6000 96GB 训练与评测。
- 数据与评分：SFT 数据源自 Toucan 单轮可依据样本；D12 主分为调用集合严格匹配，补充分为对称 schema 可选默认值等价（均为本地确定规则，见 `docs/PLAN.md` §15.12）。
- SDPO 对照：同时验证「只给校验错误」（`E2a`）、「含答案 + 成功示范」（`E2b`）与「关闭成功示范、仅保留含答案环境反馈」（`E2b-gold-only`）。

## 关键模块
- `src/tcrl/reward.py`：调用集合严格匹配与默认值等价归一化
- `src/tcrl/tool_env.py`：调用解析、schema 校验与结构化文字反馈
- `src/tcrl/data/split.py`：按工具族划分与隔离检查
- `src/tcrl/curriculum/generate.py`、`filter.py`：失败驱动的任务生成、确定性验题与 pass@8 过滤
- `src/tcrl/sft/`、`src/tcrl/eval/`、`src/tcrl/verl_reward*.py`

## 工程红线
1. **任何数字必须来自真实实验日志与归档。** 报告、README 与文档中的数字须可追溯到具体 `run_id` 与实验卡，禁止编造或预填结果。
2. **封存测试集隔离：** `test_seen` / `test_unseen` 的工具族及 BFCL 题目严禁进入任何训练或课程数据，`tests/test_split_isolation.py` 负责校验。
3. **上游代码隔离：** 依赖固定 SHA 的 `third_party/SDPO`（`7c457fc`），不直接篡改本地数据划分。
4. **不在本地运行训练，也不执行模型生成的任意代码。** 本地只做代码编辑、单元测试、数据处理与统计分析。
