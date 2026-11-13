# D3 · SFT v2 与 RL 启动验收

状态：GPU 实验已完成，日志与逐题输出已拉回。P0 未通过，出题语义人工核查待完成。

## 起点与改动

- 从 Qwen3-4B-Base 重训公共起点 E0，保留现有训练池、SFT 数据及二值奖励。v2 改为初始化并训练 6 个特殊 token；v1 故障日志保留在 `runs/d3_*_v1_frozen/`。
- 沿用 SFT 脚本默认超参，`--save-steps 50`。训练目录 `d3_sft_q3-4b-base_lora_v2`，合并目标 `/root/autodl-tmp/models/d3_sft_merged`。开跑时记录实际命令、模型来源、数据校验值与环境。
- 实例为 RTX PRO 6000 Blackwell Server Edition（97887 MiB），SDPO `7c457fc`。Base 从 `/root/autodl-fs/tcrl/models/Qwen3-4B-Base` 复制到临时盘。第一次启动早于复制完成，日志保存为 `sft_pre_copy.log`，复制完成后从头重启。
- 已核对本地与远端 SHA-256 一致：SFT `219755db0dfa0e69069d277033c57355e616a2a1a7273dfd4db6a92795cbfce0`，dev `0d862fe5e4c5203cbea1963e47cf90d8cd121b937d870f1c489ce0383f27bc1c`，RL `ccec8d26ca17faf9b8e8b42b2123d01600ee24462d8abf13e72e72de410cc021`。

## 验收与记录

| 项目 | 设置与记录 |
|---|---|
| Merge 校验 | 固定 dev 前 100 条。同一模板、EOS 和 greedy 解码下，比较挂载 LoRA（同时加载 `special_tokens.pt`）与 FP32 合并后转 BF16 的逐条输出、严格得分和停止原因。 |
| E0 greedy | `d3_e0_sft_dev_greedy`。记录 dev_seen、dev_unseen、调用题及拒绝题的严格准确率，另记输出截断率。 |
| P0 | `d3_p0_sft_rl_probe_n8`。现有 `rl.parquet`，G=8、temperature=1.0、top-p=1.0、提示上限 4096 token、响应上限 512 token，匹配 RL 的长度过滤与采样。用混合组整数计数除以总组数判定是否 ≥20%，同时报告全错/混合/全对组、pass@1、pass@8、差值与剩余错误救回率。 |
| 出题 | 从 RL 训练族抽取 50 道有参数调用题。使用 SFT 池中固定种子抽取的 3 个示例，各自附带工具定义。分别记录格式、参数有据和人工语义核查结果。 |
| RL 吞吐 | 从同一 E0 跑 GRPO 与 SDPO 各 5 步。分开记录初始化、训练、评测与存盘用时，记录单步耗时、输出长度、峰值显存及实例单价。 |

P0 按 PLAN §15.13 判定。若未过门槛，记录失败组分布再执行训练池内部的预案。封存测试集留到配置冻结后。

## 已核对结果

- `d3_sft_q3-4b-base_lora_v2`：306 步，训练日志累计 1839.5 秒（平均 6.01 秒/步），PyTorch 峰值分配 53.12 GiB。
- `d3_merge_check`：固定 100 条中 93 条输出逐字相同，100 条严格得分与停止判定一致，合并前后均答对 98 条。
- `d3_e0_sft_dev_greedy`：1129 条 dev 的严格准确率 93.27%，dev_seen 96.26%，dev_unseen 90.02%，调用题 92.29%，截断率 0%。
- `d3_p0_sft_rl_probe_n8`：2318 组中全错 36（1.55%）、混合 231（9.97%）、全对 2051（88.48%）。pass@1 94.96%，pass@8 98.45%，差值 3.48 个百分点，剩余错误救回率 69.16%。**Go/No-Go ③ 未通过**。混合组不足主要体现为当前训练池上的全对组过多。下一步按 §15.13 优先在 SFT/RL 训练池内部按 gold_key 去重切分，随后重跑 E0/P0。
- `d3_gen_check`：50/50 格式通过，47/50 参数覆盖通过。人工语义核查待完成，见 `samples.md`。
- `d3_throughput_grpo_5step` 和 `d3_throughput_sdpo_5step` 均跑完 5 步并正常退出。第 3–5 步平均分别为 58.2 和 86.9 秒/步，`nvidia-smi` 峰值分别为 81670 和 97246 MiB。SDPO 峰值距该卡总容量仅 641 MiB。此次 SDPO 使用默认含答案反馈。
- 本地归档 `runs/d3-results-39749.tar.gz` 包含原始日志、汇总和逐题结果。合并模型与最终 LoRA 已存到 AutoDL 持久盘 `/root/autodl-fs/tcrl/`。
