"""把划分好的 jsonl 导出成训练和评测直接用的文件。

- SFT：`sft_train.jsonl`，每行 {"messages": [system, user, assistant], "task": "solve" | "generate"}。
  其中约 reverse_frac 的样本是反向出题样本（给调用写问题），由同一批 sft 数据反转得到，不用外部模型。
  目的：课程阶段要让模型自己出题，只学过答题的模型不一定会出（见 PLAN §15.6）。
- RL / 评测：verl 读的 parquet，每行
    data_source="tcrl"、prompt=[system, user]、reward_model={"style", "ground_truth"}、
    extra_info={"index", "id", "split", "kind", "tools_json", ...}。
  ground_truth 与 tools_json 都存成 JSON 字符串，避免 parquet 推断嵌套结构时各行不一致。

用法：python -m tcrl.data.export --splits data/processed/splits --out data/processed/export
"""
from __future__ import annotations

import argparse
import json
import os
import random

import pyarrow as pa
import pyarrow.parquet as pq

from ..prompts import generate_messages, solve_messages, solve_target

SEED = 20261113


def sft_rows(records: list[dict], reverse_frac: float = 0.05) -> list[dict]:
    rng = random.Random(SEED)
    rows = [
        {"messages": solve_messages(r) + [{"role": "assistant", "content": solve_target(r)}], "task": "solve", "id": r["id"]}
        for r in records
    ]
    calls = [r for r in records if r["kind"] == "call"]
    n_rev = int(round(reverse_frac * len(records)))
    for r in rng.sample(calls, min(n_rev, len(calls))):
        rows.append(
            {
                "messages": generate_messages(r["tools"], r["gold"]) + [{"role": "assistant", "content": r["question"]}],
                "task": "generate",
                "id": r["id"],
            }
        )
    rng.shuffle(rows)
    return rows


def rl_rows(records: list[dict], split: str) -> list[dict]:
    out = []
    for i, r in enumerate(records):
        out.append(
            {
                "data_source": "tcrl",
                "prompt": solve_messages(r),
                "ability": "tooluse",
                "reward_model": {"style": "tcrl", "ground_truth": json.dumps(r["gold"], ensure_ascii=False)},
                "extra_info": {
                    "index": i,
                    "id": r["id"],
                    "split": split,
                    "kind": r["kind"],
                    "n_calls": len(r["gold"]),
                    "gold_seen_in_train": bool(r.get("gold_seen_in_train", False)),
                    "tools_json": json.dumps(r["tools"], ensure_ascii=False),
                },
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--splits", default="data/processed/splits")
    ap.add_argument("--out", default="data/processed/export")
    ap.add_argument("--reverse-frac", type=float, default=0.05)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    def load(name):
        return [json.loads(x) for x in open(f"{a.splits}/{name}.jsonl", encoding="utf-8")]

    sft = sft_rows(load("sft"), a.reverse_frac)
    with open(f"{a.out}/sft_train.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for row in sft:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("sft_train", len(sft), "of which generate:", sum(r["task"] == "generate" for r in sft))

    for name in ("rl", "dev_seen", "dev_unseen", "test_seen", "test_unseen"):
        rows = rl_rows(load(name), name)
        pq.write_table(pa.Table.from_pylist(rows), f"{a.out}/{name}.parquet")
        print(name, len(rows))
    # dev 合并一份，训练过程中选 checkpoint 用
    dev = rl_rows(load("dev_seen"), "dev_seen") + rl_rows(load("dev_unseen"), "dev_unseen")
    pq.write_table(pa.Table.from_pylist(dev), f"{a.out}/dev.parquet")
    print("dev", len(dev))


if __name__ == "__main__":
    main()
