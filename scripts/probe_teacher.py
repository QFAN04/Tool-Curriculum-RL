"""Compare frozen E0 teacher prompts at a validated wrong argument token."""
import argparse
import json
import random
import re
from pathlib import Path

from tcrl.prompts import CHAT_KWARGS, solve_messages
from tcrl.reward import calls_match, compute_score
from tcrl.tool_env import parse_tool_calls


def read_jsonl(path):
    with open(path, encoding="utf-8") as stream:
        return list(map(json.loads, stream))


def cases(records, samples, tokenizer, limit):
    by_id = {row["id"]: row for row in records}
    rng = random.Random(20261114)
    rng.shuffle(samples)
    for sample in samples:
        row = by_id[sample["id"]]
        gold = row["gold"]
        if len(gold) != 1:
            continue
        for output, score in zip(sample["outputs"], sample["scores"]):
            parsed = parse_tool_calls(output)
            if score or not parsed.ok or len(parsed.calls) != 1:
                continue
            pred = parsed.calls[0]
            args = gold[0]["arguments"]
            if pred["name"] != gold[0]["name"] or pred["arguments"].keys() != args.keys():
                continue
            keys = [key for key in args if pred["arguments"][key] != args[key]]
            if len(keys) != 1:
                continue
            key = keys[0]
            if isinstance(args[key], (dict, list)):
                continue
            matches = list(re.finditer(re.escape(json.dumps(key, ensure_ascii=False)) + r'\s*:\s*', output))
            if len(matches) != 1:
                continue
            start = matches[0].end()
            _, length = json.JSONDecoder().raw_decode(output[start:])
            fixed = output[:start] + json.dumps(args[key], ensure_ascii=False) + output[start + length:]
            if not calls_match(parse_tool_calls(fixed).calls, gold):
                continue
            encoding = tokenizer(output, add_special_tokens=False, return_offsets_mapping=True)
            wrong = encoding.input_ids
            right = tokenizer.encode(fixed, add_special_tokens=False)
            at = next((i for i, (a, b) in enumerate(zip(wrong, right)) if a != b), None)
            if at is None or not start <= encoding.offset_mapping[at][0] < start + length:
                continue
            yield row, output, pred, key, wrong[:at], wrong[at], right[at]
            limit -= 1
            break
        if not limit:
            return


def main():
    import torch
    import yaml
    from transformers import AutoModelForCausalLM, AutoTokenizer

    ap = argparse.ArgumentParser()
    for name in ("model", "records", "samples", "actor-config", "out"):
        ap.add_argument(f"--{name}", required=True)
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()
    tok = AutoTokenizer.from_pretrained(args.model)
    with open(args.actor_config, encoding="utf-8") as stream:
        config = yaml.safe_load(stream)["self_distillation"]
    selected = list(cases(read_jsonl(args.records), read_jsonl(args.samples), tok, args.limit))
    if not selected:
        raise ValueError("No aligned parameter-value errors found")
    print(f"Aligned cases: {len(selected)}", flush=True)
    model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.bfloat16, device_map={"": 0}).eval()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    totals = {}
    with torch.inference_mode(), (out / "samples.jsonl").open("w", encoding="utf-8") as stream:
        for row, output, pred, key, prefix, bad, good in selected:
            messages = solve_messages(row)
            feedback = compute_score(output, row["gold"], row["tools"])["feedback"]
            attempt = json.dumps(pred, ensure_ascii=False)
            actionable = f"{feedback}\nAttempted: {attempt}\nCheck the value of parameter {key!r}."
            variants = {
                "student": None,
                "current": feedback,
                "actionable": actionable,
                "gold": actionable + "\nExpected: " + json.dumps(row["gold"], ensure_ascii=False),
            }
            results = {}
            for name, raw in variants.items():
                prompt = messages
                if raw is not None:
                    question = config["reprompt_template"].format(
                        prompt=messages[-1]["content"], solution="",
                        feedback=config["feedback_template"].format(feedback_raw=raw),
                    )
                    prompt = [*messages[:-1], {"role": "user", "content": question}]
                ids = tok.apply_chat_template(prompt, tokenize=True, add_generation_prompt=True, **CHAT_KWARGS)
                if len(ids) > 6144:
                    break
                logits = model(torch.tensor([ids + prefix], device=model.device),
                               use_cache=False, logits_to_keep=1).logits[0, -1].float()
                lp = logits.log_softmax(dim=-1)
                if name == "student":
                    support = logits.topk(100).indices
                    student_lp = lp[support]
                # Include the probability mass outside the student's top-100.
                a = student_lp.exp()
                b = lp[support].exp()
                a = torch.cat((a, (1 - a.sum()).clamp_min(0).reshape(1)))
                b = torch.cat((b, (1 - b.sum()).clamp_min(0).reshape(1)))
                m = (a + b) / 2
                js = ((torch.special.xlogy(a, a) + torch.special.xlogy(b, b)
                       - torch.special.xlogy(a + b, m)).sum() / 2).item()
                results[name] = [lp[bad].item(), lp[good].item(), js]
            if len(results) != len(variants):
                continue
            for name, values in results.items():
                if name != "student":
                    totals.setdefault(name, []).append([x - y for x, y in zip(values, results["student"])])
            stream.write(json.dumps({"id": row["id"], "parameter": key, "results": results,
                                     "gold_on_student_top100": bool((support == good).any())}) + "\n")
            stream.flush()
    if not totals:
        raise ValueError("All aligned cases exceeded the teacher prompt limit")
    summary = {
        "selected": len(selected),
        "evaluated": len(totals["current"]),
        "mean_bad_logp_delta_good_logp_delta_js": {
            name: [sum(col) / len(col) for col in zip(*rows)] for name, rows in totals.items()
        },
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
