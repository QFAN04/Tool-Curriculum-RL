"""工具调用环境的确定性部分：解析模型输出、归一化调用、按 schema 校验、生成文字反馈。

约定的调用表示（全项目统一）::

    {"name": "get_weather", "arguments": {"city": "Paris", "days": 3}}

模型输出用 Qwen / Hermes 格式：每个调用放在一对 <tool_call>...</tool_call> 里，内容是一个 JSON 对象。
工具定义用 OpenAI 风格：{"name", "description", "parameters": <JSON Schema object>}，
外面套不套 {"type": "function", "function": {...}} 都接受。

这里不执行任何工具，也不调用 LLM。
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
# 有开标签但没有闭合：算格式错误，不能悄悄忽略
OPEN_TAG_RE = re.compile(r"<tool_call>")

Call = dict[str, Any]


@dataclass
class ParseResult:
    calls: list[Call] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_tool_calls(text: str) -> ParseResult:
    """从模型输出里取出全部工具调用。任何一个块解析失败都会记入 errors。"""
    res = ParseResult()
    blocks = TOOL_CALL_RE.findall(text)
    if len(OPEN_TAG_RE.findall(text)) != len(blocks):
        res.errors.append("unclosed <tool_call> tag")
    for i, raw in enumerate(blocks):
        try:
            obj = json.loads(raw.strip())
        except json.JSONDecodeError as e:
            res.errors.append(f"call {i}: invalid JSON ({e.msg})")
            continue
        if not isinstance(obj, dict):
            res.errors.append(f"call {i}: not a JSON object")
            continue
        name = obj.get("name")
        args = obj.get("arguments", {})
        if isinstance(args, str):  # 有的模型把 arguments 再编码成字符串
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                res.errors.append(f"call {i}: arguments is a string that is not valid JSON")
                continue
        if not isinstance(name, str) or not name:
            res.errors.append(f"call {i}: missing function name")
            continue
        if not isinstance(args, dict):
            res.errors.append(f"call {i}: arguments is not a JSON object")
            continue
        res.calls.append({"name": name, "arguments": args})
    return res


_TYPO_MAP = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
                           "‘": "'", "’": "'", "“": '"', "”": '"'})


def _norm_value(v: Any) -> Any:
    """值的归一化：只做不改变含义的事。

    - 字符串去掉首尾空白，并且不区分大小写（数据里同一个城市有时写 Tokyo 有时写 tokyo，
      人工核查时发现这是纯噪声）。函数名和参数名仍然区分大小写；
    - 整数值的浮点数（5.0）与整数（5）视为相同；bool 不参与这条（True 不等于 1）；
    - dict 递归并按键排序；list 保持顺序（顺序在多数 API 里有含义）。
    不做的事：字符串与数字互转、同义改写。
    """
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        # 排版字符统一：各种连字符 -> "-"，弯引号 -> 直引号（问题里常是排版字符，标准答案里是 ASCII）
        t = unicodedata.normalize("NFKC", v).translate(_TYPO_MAP)
        return t.strip().casefold()
    if isinstance(v, dict):
        return {k: _norm_value(v[k]) for k in sorted(v)}
    if isinstance(v, list):
        return [_norm_value(x) for x in v]
    return v


def canonical(call: Call) -> str:
    """调用的规范字符串，用于比较和计数。"""
    return json.dumps(
        {"name": call["name"].strip(), "arguments": _norm_value(call.get("arguments", {}))},
        sort_keys=True,
        ensure_ascii=False,
    )


def unwrap_tool(tool: dict) -> dict:
    """接受 {"type": "function", "function": {...}} 或直接 {...}。"""
    return tool["function"] if "function" in tool and isinstance(tool["function"], dict) else tool


def tool_index(tools: list[dict]) -> dict[str, dict]:
    return {unwrap_tool(t)["name"]: unwrap_tool(t) for t in tools}


_JSON_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, float) and v.is_integer()),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _type_ok(value: Any, spec: dict) -> bool:
    t = spec.get("type")
    if t is None:
        return True
    types = t if isinstance(t, list) else [t]
    return any(_JSON_TYPES.get(x, lambda v: True)(value) for x in types)


def validate_call(call: Call, tools: list[dict]) -> list[str]:
    """按工具 schema 检查一个调用，返回错误信息列表（空表示通过）。

    只检查顶层参数：函数是否存在、必填参数、未声明的参数、类型、枚举。
    不检查参数值在语义上对不对——那要和标准答案比。
    """
    idx = tool_index(tools)
    name = call["name"]
    if name not in idx:
        return [f"unknown function '{name}'"]
    params = idx[name].get("parameters") or {}
    props: dict = params.get("properties") or {}
    required: list = params.get("required") or []
    args: dict = call.get("arguments", {})
    errs = []
    for r in required:
        if r not in args:
            errs.append(f"{name}: missing required parameter '{r}'")
    for k, v in args.items():
        if k not in props:
            errs.append(f"{name}: unexpected parameter '{k}'")
            continue
        spec = props[k] if isinstance(props[k], dict) else {}
        if not _type_ok(v, spec):
            errs.append(f"{name}: parameter '{k}' should be of type {spec.get('type')}")
        elif "enum" in spec and v not in spec["enum"]:
            errs.append(f"{name}: parameter '{k}' must be one of {spec['enum']}")
    return errs


def validation_feedback(text: str, tools: list[dict]) -> str:
    """只依据输出本身和工具 schema 的反馈（不含标准答案）。没有问题时返回空串。"""
    parsed = parse_tool_calls(text)
    parts = list(parsed.errors)
    for c in parsed.calls:
        parts.extend(validate_call(c, tools))
    return "; ".join(parts)
