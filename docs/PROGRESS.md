# PROGRESS

日期 | 第几个工作日 | 可见产出（附 run_id 或文件路径）| 卡点 | 明天计划
---|---|---|---|---
2026-11-12 | D1（桌面部分）| docs/notes/d1_base_survey.md：SDPO 源码与四个参考仓库 issue 调研；submodule 固定 7c457fc | GPU 冒烟未做（需开 AutoDL 实例）| 开实例装环境，跑 GRPO / SDPO 各 10 步冒烟
2026-11-12 | D1（GPU 部分）| docs/experiment_cards/d1_smoke.md：GRPO / SDPO 0.6B LoRA 冒烟均通过（d1_smoke_grpo_q3-0p6b_lora、d1_smoke_sdpo_q3-0p6b_lora）；4B-Base SDPO 显存 68.8 GB（d1_mem_sdpo_q3-4b-base_lora）；Go/No-Go ① 通过 | 4B 未 SFT 时单步 934 s，吞吐需 D3 重测 | D2：下载 Toucan SFT 子集，定数据格式与工具族划分，写 tool_env / reward 与单测
2026-11-13 | D2 | docs/experiment_cards/d2_data.md：数据构建与工具族划分完成（调用样本 14,875 条；sft 2,187 / rl 2,188 / test_unseen 1,924 / test_seen 1,176）；tool_env、reward、split 及 36 项单测通过；核查表 docs/notes/d2_audit_100.md | 100 条人工核查待用户完成 | D3：SFT 数据渲染（含反向出题样本）、SFT 训练脚本、接入 SDPO 的奖励入口；需开机
2026-11-13 | D2（续）| 用户核查 100 条 + 子 agent 复标 + 用户抽查 20 条；过滤规则收紧（调用样本 13,827）；Go/No-Go ② 按 90% 门槛通过（用户决定）；D3 本地准备：prompts.py、data/export.py、verl_reward.py、sft/train_sft.py，44 项单测通过 | train_sft.py 未在 GPU 上运行过 | D3 开机：SFT → 合并 → probe（全错/混合/全对比例）→ 吞吐重测 → 出题验收
2026-11-13 | D3（上半场：基线与特殊 token 故障修复）| 基线日志 `runs/d3_b0_base_fewshot3_dev_greedy`、`runs/d3_b1_instruct_dev_greedy_native`、`runs/d3_b1_instruct_dev_greedy_ourprompt`；备份并定位首轮 SFT 乱码故障（`runs/d3_sft_q3-4b-base_lora_v1_frozen`、`runs/d3_e0_sft_dev_greedy_v1_frozen`：严格 `acc.all=0.1497`、`kind=call=0.0`、`truncated_fraction=1.0`，原因为 `Qwen3-4B-Base` 冻结的特殊 token `151644..151669` 共享塌缩向量与 `\tNdrFc` 冲突）；在 `src/tcrl/sft/train_sft.py` 加入 6 个特殊 token 子词均值初始化与 FP32 6 行梯度路由，修复 `scripts/autodl/d3_rl.sh`，45 项单测通过并同步至 AutoDL；完成 `docs/reviews/2026-11-13_d3_astra.md` | 实例当前已关机，待蹲卡重跑 D3 v2 | 开机重跑 SFT v2 → merge 校验 → `dev` 评测 → `rl.parquet` `P0` probe → 50 题出题验收 → 5 步吞吐实测
2026-11-13 | D3（下半场） | SFT v2 306 步；E0 dev 严格准确率 93.27%，dev_unseen 90.02%，截断率 0%；合并前后 100 条逐题得分一致；GRPO/SDPO 各 5 步跑通；日志归档 `runs/d3-results-39749.tar.gz`，详见 `docs/experiment_cards/d3_sft_probe.md` | P0 混合组 9.97%，低于 20%；50 题语义人工核查待完成 | 按 §15.13 在训练池内部做 gold_key 去重切分，再跑 E0/P0
2026-11-13 | D3（去重重测） | E0 dev 93.00%，dev_unseen 89.28%；P0 混合 240/2318（10.35%）；日志已回收，关机命令返回 0，详见 `docs/experiment_cards/d3_gold_disjoint.md` | 低于 20%；CLI 恢复桌面会话遇到写入冲突 | 按 §15.13 评估更早的 SFT checkpoint，再决定是否开卡重测
2026-11-13 | D4 | GRPO 30 步完成，末段平均 8.5 个混合组/步；dev_unseen 净少答对 1 题；同题配对在线 rollout 改善集中在旧混合题。归档 `runs/d4-27547.tar.gz` | 未见工具族没有净提升 | 先跑同题池 E0+ 对照，再决定是否投入 SDPO
2026-11-13 | D5 | E0+ 30 步；统一 greedy 未见族 E0 483/541、E0+ 478/541、GRPO 484/541；归档 `runs/d5-22856.tar.gz`；后台消息成功触发验收 | 两种训练均未给出明确的未见族收益 | 先复核退步类型，再决定 SDPO 试跑投入
2026-11-14 | D6 双卡短测 | GRPO 与 E2a 各 5 步通过，双卡 GRPO 1.07 倍吞吐，E2a 每卡峰值 91166 MiB；归档 `runs/d6-dual-43482.tar.gz` | 双卡 GRPO 每千条轨迹费用更高 | 正式 SDPO 待预算决定
2026-11-14 | D6 单卡短测 | E2a 完成 4 步，反馈正常，峰值余量 737 MiB，归档 `runs/d6-single-17361.tar.gz` | 16 分钟超时，E2b 未启动 | 实例已关，正式 SDPO 建议双卡，待预算决定
2026-11-14 | D6 正式反馈对照 | 单卡 E2a/E2b 各 30 步完成，dev_unseen 480/541 与 484/541；权重已校验，结果已回收，关机成功 | E2b 较 E0 仅净增 1 题 | 复核对照结论后决定下一阶段投入
2026-11-14 | D6 答案反馈复验 | 仅答案反馈 30 步，使用率 13.85%，dev_unseen 482/541；日志与权重已保存 | 无明确泛化收益 | 收口 SDPO
2026-11-14 | D7 正式课程与 BFCL | 三臂各 30 步，11 组 BFCL 全部完成。dev_unseen E0/C-S/C-U/C-F 为 483/480/469/478（共 541 题）。权重留持久盘，日志与逐题结果归档 d7-formal-17346.tar.gz，实例已执行关机 | 共同非种子混合率 C-U 14.02%、C-F 16.98%，两臂均未过 20% | 收口配对 GRPO，汇总四个研究问题
2026-11-14 | D7 分层复核 | 补录 dev 调用/拒答、靶向池 pass@k 与非种子层；C-F 未见族调用 402/460 与 E0 持平，拒答 76/81 | 配对 GRPO 门槛仍未过，C-S+、C2 短诊断与 D12 尚待完成 | 先做回放加低学习率对照，随后按实验卡执行 C2 与封存终评
2026-11-14 | D8 X2 | 完成 30 步与严格 dev/probe/BFCL，权重已校验，归档 d8-x2-17346.tar.gz | 靶向池 pass@1 87.59%，未见族多调用 32/70（E1 33/70） | 同实例接跑 C2 短程诊断
