"""D3 出题验收：SFT 后的模型能否「给定工具与标准调用，写出用户问题」。

从 rl 划分（训练族）里随机取 N 条调用样本，用 prompts.generate_messages 让模型写问题，
自动检查两件事：(1) 输出非空、不含 <tool_call>；(2) 标准调用的参数值是否都能在写出的问题里找到
（复用 data/build.py 的 grounded 规则）。语义是否对应由人工看 samples.md。

用法：python -m tcrl.curriculum.gen_check --model /path/merged --splits data/processed/splits --out runs/<run_id> --n 50
"""
from __future__ import annotations

import argparse
import json
import os
import random

from tcrl.data.build import grounded
from tcrl.prompts import CHAT_KWARGS, generate_messages, render_tools


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--splits", default="data/processed/splits")
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--seed", type=int, default=20261113)
    ap.add_argument("--few-shot", type=int, default=0)
    a = ap.parse_args()
    from transformers import AutoTokenizer
    from vllm import LLM, SamplingParams

    recs = [json.loads(x) for x in open(f"{a.splits}/rl.jsonl", encoding="utf-8")]
    recs = [r for r in recs if r["kind"] == "call" and any(c["arguments"] for c in r["gold"])]
    sample = random.Random(a.seed).sample(recs, a.n)
    tok = AutoTokenizer.from_pretrained(a.model)
    shots = []
    if a.few_shot:
        with open(f"{a.splits}/sft.jsonl", encoding="utf-8") as f:
            pool = [json.loads(line) for line in f]
        pool = [r for r in pool if r["kind"] == "call" and any(c["arguments"] for c in r["gold"])]
        examples = random.Random(a.seed).sample(pool, a.few_shot)
        for example in examples:
            _, request = generate_messages(example["tools"], example["gold"])
            shots.extend([
                {"role": "user", "content": f"<tools>\n{render_tools(example['tools'])}\n</tools>\n{request['content']}"},
                {"role": "assistant", "content": example["question"]},
            ])
    prompts = []
    for r in sample:
        system, user = generate_messages(r["tools"], r["gold"])
        prompts.append(tok.apply_chat_template([system, *shots, user], tokenize=False,
                                               add_generation_prompt=True, **CHAT_KWARGS))
    llm = LLM(model=a.model, gpu_memory_utilization=0.85, max_model_len=12288, seed=a.seed)
    stop = [tok.convert_tokens_to_ids("<|im_end|>"), tok.convert_tokens_to_ids("<|endoftext|>")]
    outs = llm.generate(prompts, SamplingParams(temperature=a.temperature, max_tokens=512, stop_token_ids=stop, seed=a.seed))
    os.makedirs(a.out, exist_ok=True)
    ok_shape = ok_ground = 0
    md = ["# D3 出题验收样本\n"]
    for i, (r, o) in enumerate(zip(sample, outs), 1):
        q = o.outputs[0].text.strip()
        shape = bool(q) and "<tool_call>" not in q and o.outputs[0].finish_reason == "stop"
        idx = {t["name"]: t for t in r["tools"]}
        gr = shape and all(
            grounded(c["arguments"], q, (idx[c["name"]].get("parameters") or {}).get("properties") or {},
                     (idx[c["name"]].get("parameters") or {}).get("required") or ())
            for c in r["gold"]
        )
        ok_shape += shape
        ok_ground += gr
        md.append(f"## {i}. 形式{'✓' if shape else '✗'} 有据{'✓' if gr else '✗'}\n\n**标准调用**：`{json.dumps(r['gold'], ensure_ascii=False)}`\n\n"
                  f"**模型写的问题**：{q}\n\n**原问题**：{r['question']}\n\n判定：\n")
    open(os.path.join(a.out, "samples.md"), "w", encoding="utf-8").write("\n".join(md))
    s = {"n": a.n, "few_shot": a.few_shot, "shape_ok": ok_shape, "grounded_ok": ok_ground}
    json.dump(s, open(os.path.join(a.out, "summary.json"), "w"), indent=1)
    print(s)


if __name__ == "__main__":
    main()
