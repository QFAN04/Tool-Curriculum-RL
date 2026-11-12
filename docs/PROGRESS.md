# PROGRESS

日期 | 第几个工作日 | 可见产出（附 run_id 或文件路径）| 卡点 | 明天计划
---|---|---|---|---
2026-11-12 | D1（桌面部分）| docs/notes/d1_base_survey.md：SDPO 源码与四个参考仓库 issue 调研；submodule 固定 7c457fc | GPU 冒烟未做（需开 AutoDL 实例）| 开实例装环境，跑 GRPO / SDPO 各 10 步冒烟
2026-11-12 | D1（GPU 部分）| docs/experiment_cards/d1_smoke.md：GRPO / SDPO 0.6B LoRA 冒烟均通过（d1_smoke_grpo_q3-0p6b_lora、d1_smoke_sdpo_q3-0p6b_lora）；4B-Base SDPO 显存 68.8 GB（d1_mem_sdpo_q3-4b-base_lora）；Go/No-Go ① 通过 | 4B 未 SFT 时单步 934 s，吞吐需 D3 重测 | D2：下载 Toucan SFT 子集，定数据格式与工具族划分，写 tool_env / reward 与单测
2026-11-13 | D2 | docs/experiment_cards/d2_data.md：数据构建与工具族划分完成（调用样本 14,875 条；sft 2,187 / rl 2,188 / test_unseen 1,924 / test_seen 1,176）；tool_env、reward、split 及 36 项单测通过；核查表 docs/notes/d2_audit_100.md | 100 条人工核查待用户完成 | D3：SFT 数据渲染（含反向出题样本）、SFT 训练脚本、接入 SDPO 的奖励入口；需开机
2026-11-13 | D2（续）| 用户核查 100 条 + 子 agent 复标 + 用户抽查 20 条；过滤规则收紧（调用样本 13,827）；Go/No-Go ② 按 90% 门槛通过（用户决定）；D3 本地准备：prompts.py、data/export.py、verl_reward.py、sft/train_sft.py，44 项单测通过 | train_sft.py 未在 GPU 上运行过 | D3 开机：SFT → 合并 → probe（全错/混合/全对比例）→ 吞吐重测 → 出题验收
