"""把 Toucan-1.5M 的 SFT 子集转成本项目的统一格式（每行一个 JSON）。

统一记录::

    {"id", "subset", "kind": "call" | "refuse", "question", "tools": [function dict...],
     "gold": [{"name", "arguments"}...], "refusal": str | None}

只保留：
- 单个用户轮次、只有一轮工具调用（可以是并行的多个调用）的样本 -> kind="call"；
- Toucan 标为 irrelevant 的样本（工具都不相关，不该调用）-> kind="refuse"。

kind="call" 的样本还要通过「有据」过滤：标准调用里的每个参数值都必须能在用户问题里找到
（枚举值、布尔值除外）。原因：奖励是完全匹配，像「给图片写一段描述」这种自由发挥的参数
没有唯一答案，留着只会制造噪声。

用法：python -m tcrl.data.build --raw data/raw/toucan_sft --out data/processed/all.jsonl
"""
from __future__ import annotations

import argparse
import ast
import collections
import glob
import json
import re

import pyarrow.parquet as pq

from ..tool_env import validate_call

MAX_PROMPT_CHARS = 12000  # 工具定义 + 问题的字符数上限，去掉极长的样本
MAX_REFUSAL_CHARS = 600


def _norm(s) -> str:
    # \W 在 Python 里按 Unicode 判断，中文、韩文等不会被当成标点删掉
    return re.sub(r"[\W_]+", " ", str(s).lower()).strip()


_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)


def clean_question(q: str) -> str:
    """Toucan 的改写样本里混有 HTML 注释（生成时的备注）。注释外还有正文就删掉注释；整段都在注释里就取注释内容。"""
    outside = _COMMENT_RE.sub("", q).strip()
    if outside:
        return outside
    inner = " ".join(x.strip() for x in _COMMENT_RE.findall(q)).strip()
    return inner or q.strip()


def _leaves(v):
    if isinstance(v, dict):
        for x in v.values():
            yield from _leaves(x)
    elif isinstance(v, list):
        for x in v:
            yield from _leaves(x)
    else:
        yield v


MAX_FREE_TEXT_WORDS = 5
_QUOTES = "\"'“”‘’`「」『』"
# 拒绝样本里如果带这类通用工具（能执行代码、跑命令、搜索），「工具都帮不上忙」就不成立
_GENERIC_TOOL_RE = re.compile(r"(execute|exec|run|command|shell|code|eval|search|query|browse|fetch|scrape|think|ask|chat|llm)", re.I)


def quoted_verbatim(value: str, question: str) -> bool:
    """value 是否在问题里被引号原样括起来。"""
    v = value.strip()
    start = question.find(v)
    while start != -1:
        before = question[start - 1] if start > 0 else ""
        after = question[start + len(v)] if start + len(v) < len(question) else ""
        if before in _QUOTES and before and after in _QUOTES and after:
            return True
        start = question.find(v, start + 1)
    return False


def grounded(args: dict, question: str, props: dict, required: tuple | list = ()) -> bool:
    """标准调用的参数是否都能从问题里唯一确定。规则（2026-11-13 人工核查后收紧）：

    1. 每个叶子值都要在问题里出现（归一化后的整词匹配）。
    2. 枚举值、布尔值、None：**必填**参数可以不出现在问题里（必须选一个）；
       **可选**参数则必须出现，否则「填不填都行」，答案不唯一。
    3. 超过 MAX_FREE_TEXT_WORDS 个词的字符串只有在问题里被引号原样括起来时才算数。
       否则从哪截到哪不唯一（例如把一段业务描述当作 idea 参数）。
    """
    q = f" {_norm(question)} "
    for k, v in args.items():
        spec = props.get(k) if isinstance(props.get(k), dict) else {}
        is_required = k in required
        for leaf in _leaves(v):
            if isinstance(leaf, bool) or leaf is None:
                if is_required:
                    continue
                return False
            if "enum" in spec and leaf in spec["enum"] and is_required:
                continue
            ln = _norm(leaf)
            if ln and f" {ln} " not in q:
                return False
            if isinstance(leaf, str) and len(leaf.split()) > MAX_FREE_TEXT_WORDS and not quoted_verbatim(leaf, question):
                return False
    return True


def first_paragraph(text: str) -> str:
    """拒绝样本的目标回复只取第一段，并限制长度（原回复常常有上千字）。"""
    p = text.strip().split("\n\n")[0].strip()
    return p[:MAX_REFUSAL_CHARS]


def convert(row: dict, stats: collections.Counter) -> dict | None:
    msgs = json.loads(row["messages"])
    tools = [t["function"] for t in json.loads(row["tools"])]
    if len(row["tools"]) + len(row["question"]) > MAX_PROMPT_CHARS:
        stats["drop_too_long"] += 1
        return None
    question = clean_question(row["question"])
    base = {"id": row["uuid"], "subset": row["subset_name"], "question": question, "tools": tools}

    if row["subset_name"] == "irrelevant":
        if [m["role"] for m in msgs] != ["user", "assistant"]:
            stats["drop_irrelevant_shape"] += 1
            return None
        if any(_GENERIC_TOOL_RE.search(t["name"].rsplit("-", 1)[-1]) for t in tools):
            stats["drop_refuse_generic_tool"] += 1
            return None
        stats["keep_refuse"] += 1
        return {**base, "kind": "refuse", "gold": [], "refusal": first_paragraph(msgs[1]["content"])}

    if sum(m["role"] == "user" for m in msgs) != 1:
        stats["drop_multi_user"] += 1
        return None
    rounds, prev = 0, None
    for m in msgs:
        if m["role"] == "tool_call" and prev != "tool_call":
            rounds += 1
        prev = m["role"]
    if rounds != 1:
        stats["drop_not_one_round"] += 1
        return None

    gold = []
    for m in msgs:
        if m["role"] != "tool_call":
            continue
        try:
            d = ast.literal_eval(m["content"])  # Toucan 存的是 Python 字面量字符串
            a = d["arguments"]
            a = json.loads(a) if isinstance(a, str) else a
            assert isinstance(d["name"], str) and isinstance(a, dict)
        except Exception:
            stats["drop_parse"] += 1
            return None
        gold.append({"name": d["name"], "arguments": a})

    idx = {t["name"]: t for t in tools}
    for c in gold:
        if validate_call(c, tools):  # 标准答案自己都过不了 schema 的不要
            stats["drop_gold_invalid"] += 1
            return None
        params = idx[c["name"]].get("parameters") or {}
        if not grounded(c["arguments"], question, params.get("properties") or {}, params.get("required") or ()):
            stats["drop_ungrounded"] += 1
            return None
    stats["keep_call"] += 1
    return {**base, "kind": "call", "gold": gold, "refusal": None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw/toucan_sft")
    ap.add_argument("--out", default="data/processed/all.jsonl")
    args = ap.parse_args()
    import os

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    stats: collections.Counter = collections.Counter()
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        for path in sorted(glob.glob(f"{args.raw}/**/*.parquet", recursive=True)):
            for row in pq.read_table(path).to_pylist():
                rec = convert(row, stats)
                if rec is not None:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(dict(sorted(stats.items())), indent=1))


if __name__ == "__main__":
    main()
