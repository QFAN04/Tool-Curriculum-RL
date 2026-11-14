"""用 vLLM 在某个 parquet（verl 格式，见 data/export.py）上采样并打分。只在 GPU 机器上运行。

两种用法：
  评测：--n 1 --temperature 0（greedy），报告完全匹配准确率，按 split / kind / 调用数分层。
  probe：--n 8 --temperature 1.0，额外报告每题 8 次里全错 / 混合 / 全对的比例（PLAN §15.3）。

输出：
  <out>/samples.jsonl  每题一行：id、split、kind、各次输出、各次得分
  <out>/summary.json   汇总数字

用法：
  python -m tcrl.eval.run_eval --model /path/merged --data data/processed/export/dev.parquet --out runs/<run_id> --n 1 --temperature 0
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import time

import pyarrow.parquet as pq

from tcrl.prompts import CHAT_KWARGS
from tcrl.reward import compute_score
from tcrl.thinking import thinking_stats, unpack_completion


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--top-p", type=float, default=1.0)
    ap.add_argument("--max-tokens", type=int, default=None, help="默认 greedy 1024，probe 512（与 D3 RL 一致）")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 题（调试用）")
    ap.add_argument("--max-prompt-tokens", type=int, default=None, help="只评估提示词长度不超过此值的题")
    ap.add_argument("--seed", type=int, default=20261113)
    ap.add_argument("--gpu-mem", type=float, default=0.85)
    ap.add_argument("--few-shot", type=int, default=0, help="Base 模型基线用：在 system 后插入 k 个答题示例")
    ap.add_argument("--few-shot-data", default="data/processed/export/sft_train.jsonl")
    ap.add_argument("--native-tools", action="store_true",
                    help="官方 Instruct 基线用：不用我们的 system 提示，改用模型自带对话模板的 tools= 参数（它训练时见过的格式）")
    ap.add_argument("--enable-thinking", action="store_true")
    ap.add_argument("--top-k", type=int, default=-1)
    ap.add_argument("--graded-diagnostics", action="store_true")
    ap.add_argument("--also-sample-eight-to", help="Reuse the greedy engine for the fixed D12 n=8 decode")
    a = ap.parse_args()
    if a.also_sample_eight_to and (a.n != 1 or a.temperature != 0 or a.enable_thinking):
        ap.error("--also-sample-eight-to requires direct greedy decoding")
    chat_kwargs = {**CHAT_KWARGS, "enable_thinking": a.enable_thinking}
    if a.max_tokens is None:
        a.max_tokens = 512 if a.n > 1 else 1024

    from transformers import AutoTokenizer
    from vllm import LLM, SamplingParams

    rows = pq.read_table(a.data).to_pylist()
    if a.limit:
        rows = rows[: a.limit]
    if not rows:
        ap.error("empty evaluation data")
    tok = AutoTokenizer.from_pretrained(a.model)
    shots = []
    if a.few_shot:
        pool = [json.loads(x) for x in open(a.few_shot_data, encoding="utf-8")]
        pool = [r for r in pool if r["task"] == "solve"]
        for r in pool[: a.few_shot]:  # 固定取前 k 条，可复现
            shots += [r["messages"][1], r["messages"][2]]

    def render(r):
        if a.native_tools:
            tools = [{"type": "function", "function": t} for t in json.loads(r["extra_info"]["tools_json"])]
            return tok.apply_chat_template(r["prompt"][1:], tools=tools, tokenize=False, add_generation_prompt=True,
                                           **chat_kwargs)
        msgs = list(r["prompt"])
        msgs = msgs[:1] + shots + msgs[1:]
        return tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, **chat_kwargs)

    prompts = [render(r) for r in rows]
    input_count = len(rows)
    if a.max_prompt_tokens is not None:
        eligible = [
            (row, prompt) for row, prompt in zip(rows, prompts)
            if len(tok.encode(prompt, add_special_tokens=False)) <= a.max_prompt_tokens
        ]
        if not eligible:
            ap.error("no prompts remain after length filtering")
        rows, prompts = map(list, zip(*eligible))
    stop_ids = [tok.convert_tokens_to_ids("<|im_end|>"), tok.convert_tokens_to_ids("<|endoftext|>")]
    llm = LLM(model=a.model, gpu_memory_utilization=a.gpu_mem, max_model_len=12288, seed=a.seed, enable_prefix_caching=True)
    evaluate(a, rows, prompts, tok, llm, SamplingParams, stop_ids, input_count)
    if a.also_sample_eight_to:
        a.out = a.also_sample_eight_to
        a.n, a.temperature, a.top_p, a.top_k, a.max_tokens = 8, 1.0, 1.0, -1, 512
        evaluate(a, rows, prompts, tok, llm, SamplingParams, stop_ids, input_count)


def evaluate(a, rows, prompts, tok, llm, sampling_params, stop_ids, input_count):
    sp = sampling_params(n=a.n, temperature=a.temperature, top_p=a.top_p, top_k=a.top_k, max_tokens=a.max_tokens,
                        stop_token_ids=stop_ids, seed=a.seed, skip_special_tokens=not a.enable_thinking)
    t0 = time.time()
    outs = llm.generate(prompts, sp)
    gen_s = time.time() - t0

    os.makedirs(a.out, exist_ok=True)
    by = collections.defaultdict(list)
    groups = collections.Counter(all_wrong=0, mixed=0, all_right=0)
    out_tok, truncated, fmt_err = 0, 0, 0
    thinking = []
    with open(os.path.join(a.out, "samples.jsonl"), "w", encoding="utf-8") as f:
        for r, o, prompt in zip(rows, outs, prompts, strict=True):
            ei = r["extra_info"]
            tools = json.loads(ei["tools_json"])
            scores, texts, graded_scores = [], [], []
            for c in o.outputs:
                final = c.text
                if a.enable_thinking:
                    final, metrics = unpack_completion(c.text, tok, prompt, c.finish_reason == "length")
                    thinking.append(metrics)
                res = (compute_score(final, r["reward_model"]["ground_truth"], tools)
                       if final is not None else {"score": 0, "incorrect_format": 1})
                if a.graded_diagnostics:
                    from tcrl.verl_reward_graded import compute_score as graded_score
                    graded_scores.append(graded_score(
                        r["data_source"], final, r["reward_model"]["ground_truth"], ei
                    )["score"] if final is not None else 0.0)
                scores.append(res["score"])
                texts.append(c.text)
                out_tok += len(c.token_ids)
                truncated += c.finish_reason == "length"
                fmt_err += res["incorrect_format"]
            acc = sum(scores) / len(scores)
            for key in ("all", ei["split"], f"kind={ei['kind']}", f"{ei['split']}|kind={ei['kind']}",
                        f"calls={min(ei['n_calls'], 3)}"):
                by[key].append(acc)
            if ei["split"].endswith("_seen") and ei["kind"] == "call":
                by[f"{ei['split']}|gold_seen_in_train={ei['gold_seen_in_train']}"].append(acc)
            groups["all_right" if acc == 1 else "all_wrong" if acc == 0 else "mixed"] += 1
            f.write(json.dumps({"id": ei["id"], "split": ei["split"], "kind": ei["kind"], "gold": r["reward_model"]["ground_truth"],
                                "outputs": texts, "scores": scores,
                                **({"graded_scores": graded_scores} if a.graded_diagnostics else {}),
                                **({"thinking": thinking[-len(scores):]} if a.enable_thinking else {})}, ensure_ascii=False) + "\n")
    n_out = sum(len(o.outputs) for o in outs)
    summary = {
        "model": a.model, "data": a.data, "n": a.n, "temperature": a.temperature, "few_shot": a.few_shot, "native_tools": a.native_tools,
        "top_p": a.top_p, "max_tokens": a.max_tokens, "seed": a.seed,
        "n_prompts": len(rows),
        "n_prompts_before_filter": input_count,
        "max_prompt_tokens": a.max_prompt_tokens,
        "acc": {k: round(sum(v) / len(v), 4) for k, v in sorted(by.items())},
        "count": {k: len(v) for k, v in sorted(by.items())},
        "group_count": dict(groups) if a.n > 1 else None,
        "group_fraction": {k: round(v / len(rows), 4) for k, v in groups.items()} if a.n > 1 else None,
        "mean_output_tokens": round(out_tok / n_out, 1),
        "truncated_fraction": round(truncated / n_out, 4),
        "format_error_fraction": round(fmt_err / n_out, 4),
        "generate_seconds": round(gen_s, 1),
    }
    if a.enable_thinking:
        summary["thinking"] = thinking_stats(thinking)
    summary["enable_thinking"] = a.enable_thinking
    summary["top_k"] = a.top_k
    if a.n > 1:
        pass_at_1 = sum(by["all"]) / len(rows)
        pass_at_n = 1 - groups["all_wrong"] / len(rows)
        summary.update({
            "pass_at_1": round(pass_at_1, 4),
            f"pass_at_{a.n}": round(pass_at_n, 4),
            f"pass_at_{a.n}_minus_pass_at_1": round(pass_at_n - pass_at_1, 4),
            "remaining_error_recovery": (
                round((pass_at_n - pass_at_1) / (1 - pass_at_1), 4) if pass_at_1 < 1 else None
            ),
        })
    json.dump(summary, open(os.path.join(a.out, "summary.json"), "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
