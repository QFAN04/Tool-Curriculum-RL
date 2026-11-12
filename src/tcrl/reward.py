"""奖励：模型输出的调用多重集与标准答案完全一致记 1，否则 0。

「多重集」= 不看调用之间的先后顺序，但看每个调用出现的次数。
标准答案为空列表表示这道题不该调用工具，此时输出里不能有任何 <tool_call>。

compute_score 的返回字段与 SDPO 上游的打分器一致（score / acc / pred / incorrect_format / feedback），
方便之后挂到 verl 的 custom_reward_function 上。
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .tool_env import Call, canonical, parse_tool_calls, validate_call


def calls_match(pred: list[Call], gold: list[Call]) -> bool:
    return Counter(canonical(c) for c in pred) == Counter(canonical(c) for c in gold)


def diff_feedback(pred: list[Call], gold: list[Call], include_gold: bool) -> str:
    """与标准答案不一致时的反馈。include_gold=False 时只说哪类不一致，不给出答案内容。"""
    p, g = Counter(canonical(c) for c in pred), Counter(canonical(c) for c in gold)
    if p == g:
        return ""
    pn, gn = Counter(c["name"] for c in pred), Counter(c["name"] for c in gold)
    parts = []
    if not gold:
        parts.append("no tool call was expected")
    elif not pred:
        parts.append("a tool call was expected but none was made")
    elif pn != gn:
        if sum(pn.values()) != sum(gn.values()):
            parts.append(f"wrong number of tool calls ({sum(pn.values())} made)")
        if set(pn) != set(gn):
            parts.append("wrong function selected")
    else:
        parts.append("function names are right but argument values differ")
    if include_gold:
        parts.append("expected: " + json.dumps(gold, ensure_ascii=False, sort_keys=True))
    return "; ".join(parts)


def compute_score(
    solution: str,
    ground_truth: str | list[Call],
    tools: list[dict] | None = None,
    include_gold_in_feedback: bool = False,
) -> dict[str, Any]:
    gold: list[Call] = json.loads(ground_truth) if isinstance(ground_truth, str) else ground_truth
    parsed = parse_tool_calls(solution)
    correct = parsed.ok and calls_match(parsed.calls, gold)

    fb = list(parsed.errors)
    if tools is not None:
        for c in parsed.calls:
            fb.extend(validate_call(c, tools))
    if not correct:
        d = diff_feedback(parsed.calls, gold, include_gold_in_feedback)
        if d:
            fb.append(d)

    return {
        "score": 1.0 if correct else 0.0,
        "acc": 1.0 if correct else 0.0,
        "pred": json.dumps(parsed.calls, ensure_ascii=False, sort_keys=True),
        "incorrect_format": 0 if parsed.ok else 1,
        "feedback": "" if correct else "; ".join(fb),
    }
