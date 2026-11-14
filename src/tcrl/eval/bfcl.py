"""Run the fixed BFCL Python single-turn slice with the project tool protocol."""
import argparse
from collections import Counter
from copy import deepcopy
from importlib.metadata import version
import json
from pathlib import Path

from tcrl.prompts import CHAT_KWARGS, solve_messages
from tcrl.tool_env import TOOL_CALL_RE, parse_tool_calls
from tcrl.thinking import thinking_stats, unpack_completion

CATEGORIES = ("simple", "multiple", "parallel", "parallel_multiple", "irrelevance")
BFCL_VERSION = "2026.3.23"
MODEL_CONFIG = "Qwen/Qwen3-4B-Instruct-2507"


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf8").splitlines()]


def dataset(root):
    for category in CATEGORIES:
        name = "simple_python" if category == "simple" else category
        filename = f"BFCL_v4_{name}.json"
        prompts = read(root / filename)
        answers = {} if category == "irrelevance" else {
            row["id"]: row["ground_truth"] for row in read(root / "possible_answer" / filename)
        }
        ids = {row["id"] for row in prompts}
        if len(ids) != len(prompts) or (category != "irrelevance" and ids != answers.keys()):
            raise ValueError(f"{category}: prompt and answer IDs are inconsistent")
        for row in prompts:
            messages, = row["question"]
            if any(message["role"] != "user" for message in messages):
                raise ValueError(f"{row['id']}: unexpected single-turn roles")
            yield category, row, answers.get(row["id"])


def json_schema(value):
    if isinstance(value, list):
        return [json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = deepcopy(value)
    if "type" in result:
        result["type"] = {"dict": "object", "tuple": "array"}.get(result["type"], result["type"])
    for key in ("items", "additionalProperties"):
        if key in result:
            result[key] = json_schema(result[key])
    if "properties" in result:
        result["properties"] = {key: json_schema(item) for key, item in result["properties"].items()}
    return result


def messages_for(row):
    messages, = row["question"]
    tools = [
        {**tool, "parameters": json_schema(tool["parameters"])}
        for tool in row["function"]
    ]
    return solve_messages({"question": "\n".join(message["content"] for message in messages), "tools": tools}), tools


def decode(text):
    text = text.strip()
    parsed = parse_tool_calls(text)
    if not parsed.ok or text.count("</tool_call>") != len(parsed.calls):
        return None
    if parsed.calls and TOOL_CALL_RE.sub("", text).strip():
        return None
    return [{call["name"]: call["arguments"]} for call in parsed.calls]


def score(category, row, gold, text, truncated=False):
    if truncated:
        return False
    decoded = decode(text)
    if category == "irrelevance":
        return decoded == [] and bool(text.strip())
    if not decoded:
        return False
    from bfcl_eval.constants.enums import Language
    from bfcl_eval.eval_checker.ast_eval.ast_checker import ast_checker

    return ast_checker(
        row["function"], decoded, gold, Language.PYTHON, category, MODEL_CONFIG,
    )["valid"]


def generate(args):
    from transformers import AutoTokenizer
    from vllm import LLM, SamplingParams

    entries = list(dataset(args.data))
    if args.ids:
        ids = set(args.ids.read_text(encoding="utf8").splitlines())
        entries = [entry for entry in entries if entry[1]["id"] in ids]
        if not ids or len(entries) != len(ids):
            raise ValueError("Unknown or empty BFCL ID selection")
    if args.limit:
        entries = entries[:args.limit]
    tok = AutoTokenizer.from_pretrained(args.model)
    shots = []
    if args.few_shot_data:
        examples = [row for row in read(args.few_shot_data) if row["task"] == "solve"]
        for example in examples[:3]:
            shots.extend(example["messages"][1:])
    prompts = []
    for _, row, _ in entries:
        messages, tools = messages_for(row)
        if args.native:
            messages = messages[1:]
        else:
            messages = messages[:1] + shots + messages[1:]
        kwargs = {"tools": [{"type": "function", "function": tool} for tool in tools]} if args.native else {}
        prompts.append(tok.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, **{**CHAT_KWARGS, 'enable_thinking': args.enable_thinking}, **kwargs,
        ))
    llm = LLM(model=args.model, enable_prefix_caching=True, max_model_len=12288, seed=20261114)
    outputs = llm.generate(prompts, SamplingParams(
        temperature=args.temperature, top_p=args.top_p, top_k=args.top_k, max_tokens=args.max_tokens, seed=20261114,
        skip_special_tokens=not args.enable_thinking,
        stop_token_ids=[tok.convert_tokens_to_ids(tag) for tag in ("<|im_end|>", "<|endoftext|>")],
    ))
    with args.out.open("x", encoding="utf8") as handle:
        for (_, row, _), output, prompt in zip(entries, outputs, prompts, strict=True):
            completion, = output.outputs
            truncated = completion.finish_reason == "length"
            final = completion.text
            metrics = {}
            if args.enable_thinking:
                final, detail = unpack_completion(final, tok, prompt, truncated)
                metrics = {"thinking": detail, "final": final}
            handle.write(json.dumps({
                "id": row["id"], "text": completion.text, "truncated": truncated, **metrics,
            }, ensure_ascii=False) + "\n")


def evaluate(args):
    if version("bfcl-eval") != BFCL_VERSION:
        raise ValueError(f"Install bfcl-eval=={BFCL_VERSION}")
    rows = read(args.predictions)
    outputs = {row["id"]: row for row in rows}
    entries = list(dataset(args.data))
    if args.ids:
        ids = set(args.ids.read_text(encoding="utf8").splitlines())
        entries = [entry for entry in entries if entry[1]["id"] in ids]
        if not ids or len(entries) != len(ids):
            raise ValueError("Unknown or empty BFCL ID selection")
    if args.limit:
        entries = entries[:args.limit]
    if len(outputs) != len(rows) or outputs.keys() != {row["id"] for _, row, _ in entries}:
        raise ValueError("Prediction IDs do not match the evaluation slice")
    counts, correct = Counter(), Counter()
    details = []
    for category, row, gold in entries:
        output = outputs[row["id"]]
        final = output.get("final", output["text"])
        valid = final is not None and bool(score(category, row, gold, final, output["truncated"]))
        counts[category] += 1
        correct[category] += valid
        details.append({"id": row["id"], "category": category, "correct": valid})
    summary = {key: {"n": count, "correct": correct[key], "accuracy": correct[key] / count}
               for key, count in counts.items()}
    total = sum(counts.values())
    payload = {"all": {"n": total, "correct": sum(correct.values()), "accuracy": sum(correct.values()) / total},
               "categories": summary, "samples": details}
    if rows and all("thinking" in row for row in rows):
        payload["thinking"] = thinking_stats([row["thinking"] for row in rows])
        payload["truncated_fraction"] = sum(row["truncated"] for row in rows) / len(rows)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    for command, handler in (("generate", generate), ("score", evaluate)):
        sub = commands.add_parser(command)
        sub.set_defaults(handler=handler)
        sub.add_argument("--data", type=Path, required=True)
        sub.add_argument("--out", type=Path, required=True)
        sub.add_argument("--limit", type=int)
        sub.add_argument("--ids", type=Path)
        if command == "generate":
            sub.add_argument("--model", required=True)
            sub.add_argument("--native", action="store_true")
            sub.add_argument("--enable-thinking", action="store_true")
            sub.add_argument("--temperature", type=float, default=0)
            sub.add_argument("--top-p", type=float, default=1)
            sub.add_argument("--top-k", type=int, default=-1)
            sub.add_argument("--max-tokens", type=int, default=1024)
            sub.add_argument("--few-shot-data", type=Path)
        else:
            sub.add_argument("--predictions", type=Path, required=True)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
