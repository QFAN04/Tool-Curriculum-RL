"""LoRA SFT：Qwen3-4B-Base -> 会按格式做工具调用的模型。只在 GPU 机器上运行。

做的事很少，故意不用框架：
  1. 用 tokenizer 的对话模板把 [system, user, assistant] 渲染成一条序列；
  2. 只在 assistant 的内容（含结束符）上算 loss，前面的提示部分 label 置 -100；
  3. LoRA 训练，按步保存，可 --resume。
训练完用 --merge 把 LoRA 合并进基座，得到给 RL 用的完整权重（合并用 fp32 计算再存 bf16）。

用法：
  python -m tcrl.sft.train_sft --model /path/Qwen3-4B-Base --data data/processed/export/sft_train.jsonl --out runs/<run_id>
  python -m tcrl.sft.train_sft --model /path/Qwen3-4B-Base --merge runs/<run_id>/final --out /path/merged
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import time


def build_example(tokenizer, messages: list[dict], max_len: int):
    """返回 (input_ids, labels)。提示部分用模板渲染到「该 assistant 开口」为止，答案部分单独编码后接上结束符。"""
    prompt = tokenizer.apply_chat_template(messages[:-1], tokenize=False, add_generation_prompt=True)
    p_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
    a_ids = tokenizer(messages[-1]["content"] + "<|im_end|>", add_special_tokens=False)["input_ids"]
    ids = (p_ids + a_ids)[:max_len]
    labels = ([-100] * len(p_ids) + a_ids)[:max_len]
    return ids, labels


def merge(args) -> None:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    base = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.float32)
    model = PeftModel.from_pretrained(base, args.merge).merge_and_unload()
    model.to(torch.bfloat16).save_pretrained(args.out, safe_serialization=True)
    AutoTokenizer.from_pretrained(args.model).save_pretrained(args.out)
    print("merged ->", args.out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--data")
    ap.add_argument("--out", required=True)
    ap.add_argument("--merge", help="LoRA 目录；给了就只做合并")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--grad-accum", type=int, default=2)
    ap.add_argument("--max-len", type=int, default=4096)
    ap.add_argument("--lora-rank", type=int, default=32)
    ap.add_argument("--lora-alpha", type=int, default=32)
    ap.add_argument("--save-steps", type=int, default=100)
    ap.add_argument("--max-steps", type=int, default=-1)
    ap.add_argument("--seed", type=int, default=20261113)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    if args.merge:
        return merge(args)

    import torch
    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, get_cosine_schedule_with_warmup

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    os.makedirs(args.out, exist_ok=True)
    tok = AutoTokenizer.from_pretrained(args.model)
    tok.pad_token = tok.pad_token or tok.eos_token

    rows = [json.loads(x) for x in open(args.data, encoding="utf-8")]
    data, dropped = [], 0
    for r in rows:
        ids, labels = build_example(tok, r["messages"], args.max_len)
        if all(x == -100 for x in labels):  # 提示本身就超长，答案被截没了
            dropped += 1
            continue
        data.append((ids, labels))
    steps_per_epoch = math.ceil(len(data) / (args.batch_size * args.grad_accum))
    total = args.max_steps if args.max_steps > 0 else int(steps_per_epoch * args.epochs)
    print(f"examples {len(data)} (dropped {dropped}), optimizer steps {total}")

    model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.bfloat16, device_map={"": 0})
    model.gradient_checkpointing_enable()
    start, ckpts = 0, sorted(
        (d for d in os.listdir(args.out) if d.startswith("step-")), key=lambda d: int(d.split("-")[1])
    )
    if args.resume and ckpts:
        model = PeftModel.from_pretrained(model, os.path.join(args.out, ckpts[-1]), is_trainable=True)
        start = int(ckpts[-1].split("-")[1])
    else:
        model = get_peft_model(
            model,
            LoraConfig(task_type="CAUSAL_LM", r=args.lora_rank, lora_alpha=args.lora_alpha, target_modules="all-linear"),
        )
    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.0)
    sched = get_cosine_schedule_with_warmup(opt, int(0.03 * total), total)
    state = os.path.join(args.out, ckpts[-1], "trainer_state.pt") if (args.resume and ckpts) else None
    if state and os.path.exists(state):
        st = torch.load(state)
        opt.load_state_dict(st["opt"])
        sched.load_state_dict(st["sched"])

    def batches():
        rng = random.Random(args.seed)
        while True:
            order = list(range(len(data)))
            rng.shuffle(order)
            for i in range(0, len(order) - args.batch_size + 1, args.batch_size):
                yield [data[j] for j in order[i : i + args.batch_size]]

    def collate(batch):
        m = max(len(x[0]) for x in batch)
        ids = torch.full((len(batch), m), tok.pad_token_id)
        lab = torch.full((len(batch), m), -100)
        att = torch.zeros((len(batch), m), dtype=torch.long)
        for i, (a, b) in enumerate(batch):
            ids[i, : len(a)] = torch.tensor(a)
            lab[i, : len(b)] = torch.tensor(b)
            att[i, : len(a)] = 1
        return ids.cuda(), att.cuda(), lab.cuda()

    gen = batches()
    for _ in range(start * args.grad_accum):  # 恢复时跳过已经用过的 batch，保持数据顺序一致
        next(gen)
    log = open(os.path.join(args.out, "train_log.jsonl"), "a", encoding="utf-8")
    model.train()
    t0 = time.time()
    for step in range(start + 1, total + 1):
        loss_sum = 0.0
        for _ in range(args.grad_accum):
            ids, att, lab = collate(next(gen))
            loss = model(input_ids=ids, attention_mask=att, labels=lab).loss / args.grad_accum
            loss.backward()
            loss_sum += loss.item()
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        opt.step()
        sched.step()
        opt.zero_grad(set_to_none=True)
        rec = {"step": step, "loss": loss_sum, "lr": sched.get_last_lr()[0], "elapsed_s": round(time.time() - t0, 1),
               "max_mem_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2)}
        log.write(json.dumps(rec) + "\n")
        log.flush()
        if step % 10 == 0 or step == total:
            print(rec, flush=True)
        if step % args.save_steps == 0 or step == total:
            d = os.path.join(args.out, f"step-{step}")
            model.save_pretrained(d)
            torch.save({"opt": opt.state_dict(), "sched": sched.state_dict()}, os.path.join(d, "trainer_state.pt"))
    model.save_pretrained(os.path.join(args.out, "final"))
    print("done")


if __name__ == "__main__":
    main()
