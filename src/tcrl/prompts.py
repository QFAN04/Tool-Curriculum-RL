"""提示的渲染。SFT、RL、评测、出题都从这里取，保证同一条样本在各阶段看到的输入逐字相同。

格式沿用 Qwen / Hermes 的工具调用约定，但**工具定义由我们自己写进 system 消息**，
不依赖 tokenizer 模板的 `tools=` 参数——那样不同版本的模板渲染出来可能不一样，训练和评测就对不上。
tokenizer 的对话模板只用来加角色标记。
"""
from __future__ import annotations

import json

# 渲染对话模板时统一传这组参数。SDPO 的默认配置是 enable_thinking=False；
# Qwen3-4B-Base 的模板在这个设置下会在 assistant 开头放一个空的 <think></think>，
# SFT、RL、评测必须都一样，否则训练和推理看到的前缀不同。
CHAT_KWARGS = {"enable_thinking": False}

SYSTEM_SOLVE = (
    "You are a helpful assistant with access to the following functions. "
    "Decide whether any of them is needed to answer the user.\n"
    "- If yes, reply with one <tool_call></tool_call> block per call and nothing else. "
    "Each block contains a JSON object with the keys \"name\" and \"arguments\".\n"
    "- If none of the functions is relevant, do not call any; reply to the user in plain text.\n\n"
    "<tools>\n{tools}\n</tools>"
)

SYSTEM_GENERATE = (
    "You write user requests for a tool-calling assistant. You are given the available functions and the exact "
    "function call(s) the assistant should make. Write one realistic user request for which those calls, with "
    "exactly those argument values, are the right response. Every argument value must appear in the request. "
    "Reply with the request only.\n\n"
    "<tools>\n{tools}\n</tools>"
)


def render_tools(tools: list[dict]) -> str:
    """每个工具一行 JSON；键排序，保证同一组工具渲染结果唯一。"""
    return "\n".join(
        json.dumps({"type": "function", "function": t}, ensure_ascii=False, sort_keys=True) for t in tools
    )


def render_calls(calls: list[dict]) -> str:
    return "\n".join(
        "<tool_call>\n" + json.dumps({"name": c["name"], "arguments": c["arguments"]}, ensure_ascii=False) + "\n</tool_call>"
        for c in calls
    )


def solve_messages(rec: dict) -> list[dict]:
    """答题：给问题和工具，要模型输出调用（或拒绝）。"""
    return [
        {"role": "system", "content": SYSTEM_SOLVE.format(tools=render_tools(rec["tools"]))},
        {"role": "user", "content": rec["question"]},
    ]


def solve_target(rec: dict) -> str:
    return render_calls(rec["gold"]) if rec["kind"] == "call" else rec["refusal"]


def generate_messages(tools: list[dict], calls: list[dict]) -> list[dict]:
    """出题（反向）：给工具和标准调用，要模型写出用户问题。课程生成和 SFT 的反向样本共用。"""
    return [
        {"role": "system", "content": SYSTEM_GENERATE.format(tools=render_tools(tools))},
        {"role": "user", "content": "Function call(s):\n" + render_calls(calls)},
    ]
