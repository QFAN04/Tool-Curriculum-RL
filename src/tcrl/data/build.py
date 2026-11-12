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


def grounded(args: dict, question: str, props: dict) -> bool:
    """每个参数的叶子值是否都出现在问题里（按字母数字归一化后的整词匹配）。"""
    q = f" {_norm(question)} "
    for k, v in args.items():
        spec = props.get(k) if isinstance(props.get(k), dict) else {}
        for leaf in _leaves(v):
            if isinstance(leaf, bool) or leaf is None:
                continue
            if "enum" in spec and leaf in spec["enum"]:
                continue
            ln = _norm(leaf)
            if ln and f" {ln} " not in q:
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
        props = (idx[c["name"]].get("parameters") or {}).get("properties") or {}
        if not grounded(c["arguments"], question, props):
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
