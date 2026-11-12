# CLAUDE.md — tool-curriculum-rl（第三个项目，工作名）

把 Qwen3-4B-Base 经 SFT → RL 训练成工具调用模型，并检验：等总成本下，由模型自己的失败驱动的自生成课程，能否比静态数据更好地泛化到没见过的工具。

- **新会话先读 `docs/HANDOFF.md`**（规划会话的决策理由、已否决的方案、待办清单）。
- 完整计划：`docs/PLAN.md`（v0，2026-11-12 草稿）。与本文件冲突时以本文件「已定决策」为准。
- 姊妹项目：`D:/AI-Projects/stateful-search-rl`（主，已完成）、`D:/AI-Projects/agentic-verifier-code`（副，已结项）。可复用它们的 veRL 镜像经验、检查脚本、沙箱和实验卡流程，但不改动那两个仓库。
- 当前状态（2026-11-12）：选题已定（A′），计划为草稿，**尚未运行任何实验**。

## 已定决策（变更需用户确认）
- 选题：工具调用后训练流水线（Base → SFT → RL）+ 反馈驱动的迭代课程；RSI 成分占总预算约 15–20%，最多两轮（2026-11-12 用户确认）。
- 思想借鉴 Tool-R0，对外如实说明；不做 autoresearch 式外层循环。
- 主评测为按工具族隔离的自建封存集；BFCL 只作辅助。

- 底座：`lasgroup/SDPO`（veRL 0.7.0.dev，含 GRPO 与 SDPO），submodule 固定在 `7c457fc`；GPU 冒烟不过则退回 veRL 0.7 镜像只做 GRPO（2026-11-12 用户确认）。
- RL 阶段做 GRPO vs SDPO 对照（E2）（2026-11-12 用户确认）。
- 模型 Qwen3-4B-Base + LoRA；算力 AutoDL RTX PRO 6000 96GB，每组 1 卡。
- 预算：租卡总花费以不超过主项目（约 70 卡时）为目标；确有研究价值时可以略超，超之前先向用户说明理由（2026-11-12 用户确认）。
- SDPO 的「含答案」与「只给校验错误」两种设置都跑，主表并列（2026-11-12 用户确认）。
- 实验矩阵以 PLAN §15.11 为准（§7 是修订前的版本）。
- AutoDL 私有镜像 `tcrl-sdpo-7c457fc`（2026-11-13 用户保存，源自 `ssrl-verl070-vllm012` + SDPO@7c457fc + 4 个小包）。开机后先跑 `clean_stale.py`（镜像恢复会带回残留旧文件），见 `docs/experiment_cards/d1_smoke.md`。
- 参考项目（SDPO、ToolRL、Tool-R0、R-Zero 等）用之前先查它的 issue / PR（用户要求）。

## 待确认（确认后移到上面）
- SFT 数据源：Toucan 的 SFT config 优先，xlam-60k 备选（D2 看过格式后定）。
- 项目名与目录名。

## 关键模块（★ = 用户必须亲自读懂）
- ★ `src/tcrl/reward.py`：奖励与归一化
- ★ `src/tcrl/tool_env.py`：解析、schema 校验、文字反馈
- ★ `src/tcrl/data/split.py`：按工具族划分与隔离检查
- ★ `src/tcrl/curriculum/generate.py`、`filter.py`：失败驱动的任务生成与过滤
- `src/tcrl/sft/`、`eval/`、`diagnostics.py`

## 审查节点（GPT codex 只读审查）
D2 数据划分与奖励合并后 → D3 正式开跑前 → D7 课程启动前 → D13 报告定稿。

## 节奏与 Go/No-Go（详见 docs/PLAN.md §8）
D1 底座 → D2 数据与奖励（人工核查 100 条 ≥ 95%）→ D3 SFT + probe（pass@8 − pass@1 ≥ 15 个百分点）→ D4–D6 RL → D7 RL 须优于 SFT-only 才启动课程 → D8–D11 两轮课程 → D12 封存测试 → D13–14 报告。

## 红线（任何 agent 都必须遵守）
1. **任何数字必须来自 `runs/<run_id>/` 下的真实日志。** README、报告、简历里的每个数字都标注 run_id。禁止预填、估计或把「预期结果」写成结果。
2. **一次只改一个变量。** 每个正式 run 开跑前在 `docs/experiment_cards/` 写好实验卡。
3. **封存测试集只在配置冻结后跑一次。** checkpoint 只按 dev 选。
4. **dev / test 的工具族和 BFCL 的题不得进入任何训练或课程数据。** `tests/test_split_isolation.py` 做断言。
5. **不在本地 Windows 跑训练，也不执行模型生成的代码。** 本地只做编辑、单测、数据处理、分析。
6. 上游代码放在 `third_party/`，固定 SHA；必须修改时用 patch 文件记录。
7. 不要为了让测试通过而改测试或放宽断言；先找原因。
8. 课程里的任务只由被训练的模型自己生成，不用外部强模型出题。

## 进度规则：每个工作日都要有可见产出
- 按「项目工作日」计算，不按日历日；用户没有推进的日子不计入，也不催促。
- 每个工作日结束时，必须有一个能看到的新产出：一条真实轨迹、一张曲线、一个评测数字、一个通过的关键单测、一份实验卡结果等。只搭环境、写框架、写文档，不算产出。
- 连续 2 个工作日没有可见产出：停下来，向用户说明卡在哪里并给出选项，由用户决定。
- 每个工作日结束时，在 `docs/PROGRESS.md` 追加一行：`日期 | 第几个工作日 | 可见产出（附 run_id 或文件路径）| 卡点 | 明天计划`。
- 测试范围以 PLAN §4 的清单为准；清单之外不为小功能补测试，不追求全覆盖。

## 运行规范
- 所有训练、生成、评测脚本支持 `--resume`。
- checkpoint 包含模型、optimizer、scheduler、RNG 状态、数据读取进度、step。
- 任务结束（无论成败）：同步 `runs/<run_id>/` → 关机。每个实例设 `--ttl`。
- 创建多卡实例或单次预计花费超过 ¥20 前，先向用户确认。
- 每次开机记录 ¥/h、吞吐、峰值显存到实验卡。

## 协作分工（遵循 multi-model-collaboration 协议）
- Claude 主导全局并编写核心代码；跑通实验、拿到日志是第一优先级。
- GPT（codex）：只读独立审查，只在审查节点介入。
- Gemini（通过 Antigravity CLI `agy` 调用）/ DeepSeek：只接与核心文件不相交、规格清楚的并行任务。
- 同一文件同一时间只能有一个 agent 修改；外部 agent 说「完成」后，主控必须自己看 diff、跑测试。

## 与用户协作
- 用中文交流。
- 用户的需求或观点不要下意识附和，先判断再回答，但不要过度防御。
