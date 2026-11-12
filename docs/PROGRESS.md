# PROGRESS

日期 | 第几个工作日 | 可见产出（附 run_id 或文件路径）| 卡点 | 明天计划
---|---|---|---|---
2026-11-12 | D1（桌面部分）| docs/notes/d1_base_survey.md：SDPO 源码与四个参考仓库 issue 调研；submodule 固定 7c457fc | GPU 冒烟未做（需开 AutoDL 实例）| 开实例装环境，跑 GRPO / SDPO 各 10 步冒烟
2026-11-12 | D1（GPU 部分）| docs/experiment_cards/d1_smoke.md：GRPO / SDPO 0.6B LoRA 冒烟均通过（d1_smoke_grpo_q3-0p6b_lora、d1_smoke_sdpo_q3-0p6b_lora）；4B-Base SDPO 显存 68.8 GB（d1_mem_sdpo_q3-4b-base_lora）；Go/No-Go ① 通过 | 4B 未 SFT 时单步 934 s，吞吐需 D3 重测 | D2：下载 Toucan SFT 子集，定数据格式与工具族划分，写 tool_env / reward 与单测
