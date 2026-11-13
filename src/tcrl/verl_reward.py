"""verl / SDPO 的奖励入口。在训练配置里把 custom_reward_function.path 指到这个文件。

签名与上游 `verl/utils/reward_score/feedback/__init__.py` 一致。返回的 dict 里：
  score     -> GRPO 用的标量奖励，也是 SDPO 判断「是否成功」的依据
  feedback  -> SDPO 给老师看的文字反馈（成功时为空串）

环境变量 TCRL_FEEDBACK_GOLD：
  "1"（默认）反馈里包含标准答案，对应上游做法（实验 E2b）
  "0"        反馈只有格式 / schema 校验错误和「哪类不一致」，不含答案（实验 E2a）
"""
from __future__ import annotations

import json
import os

from tcrl.reward import compute_score as _score


def compute_score(data_source: str, solution_str: str, ground_truth: str, extra_info: dict | None = None) -> dict:
    tools = None
    if extra_info and extra_info.get("tools_json"):
        tools = json.loads(extra_info["tools_json"])
    result = _score(
        solution_str,
        ground_truth,
        tools,
        include_gold_in_feedback=os.environ.get("TCRL_FEEDBACK_GOLD", "1") == "1",
    )
    if extra_info and "id" in extra_info:
        result["sample_id"] = extra_info["id"]
        result["split"] = extra_info.get("split", "")
    return result
