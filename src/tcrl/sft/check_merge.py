"""Compare adapter and merged generation on the first 100 dev rows."""
from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path

from tcrl.prompts import CHAT_KWARGS
from tcrl.reward import compute_score


def main() -> None:
    import pyarrow.parquet as pq
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    parser = argparse.ArgumentParser()
    for name in ("base", "adapter", "merged", "data", "out"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    rows = pq.read_table(args.data).slice(0, 100).to_pylist()
    tok = AutoTokenizer.from_pretrained(args.merged, padding_side="left")
    prompts = [tok.apply_chat_template(row["prompt"], tokenize=False, add_generation_prompt=True,
                                       **CHAT_KWARGS) for row in rows]
    stops = [tok.eos_token_id, tok.convert_tokens_to_ids("<|endoftext|>")]

    @torch.inference_mode()
    def generate(model):
        results = []
        for offset in range(0, len(rows), 8):
            batch = tok(prompts[offset:offset + 8], padding=True, add_special_tokens=False,
                        return_tensors="pt").to(model.device)
            generated = model.generate(**batch, do_sample=False, max_new_tokens=512,
                                       eos_token_id=stops, pad_token_id=tok.pad_token_id)
            for row, ids in zip(rows[offset:offset + 8], generated[:, batch.input_ids.shape[1]:].tolist()):
                end = next((i for i, tid in enumerate(ids) if tid in stops), len(ids))
                output = tok.decode(ids[:end], skip_special_tokens=False)
                score = compute_score(output, row["reward_model"]["ground_truth"])
                results.append({"output": output, "score": score["score"], "stopped": end < len(ids)})
        return results

    base = AutoModelForCausalLM.from_pretrained(args.base, dtype=torch.bfloat16, device_map={"": 0})
    special = torch.load(Path(args.adapter) / "special_tokens.pt", weights_only=True)
    with torch.no_grad():
        weight = base.get_input_embeddings().weight
        weight[special["ids"]] = special["weight"].to(weight)
    model = PeftModel.from_pretrained(base, args.adapter).eval()
    before = generate(model)
    del model, base, weight
    gc.collect()
    torch.cuda.empty_cache()
    after = generate(AutoModelForCausalLM.from_pretrained(
        args.merged, dtype=torch.bfloat16, device_map={"": 0}).eval())

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pairs = list(zip(before, after))
    with (out / "samples.jsonl").open("w", encoding="utf-8") as f:
        for row, (adapter, merged) in zip(rows, pairs):
            f.write(json.dumps({"id": row["extra_info"]["id"], "adapter": adapter, "merged": merged},
                               ensure_ascii=False) + "\n")
    summary = {"n": len(pairs), "output_equal": sum(a["output"] == b["output"] for a, b in pairs),
               "score_equal": sum(a["score"] == b["score"] for a, b in pairs),
               "stop_equal": sum(a["stopped"] == b["stopped"] for a, b in pairs),
               "adapter_correct": sum(a["score"] for a, _ in pairs),
               "merged_correct": sum(b["score"] for _, b in pairs)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
