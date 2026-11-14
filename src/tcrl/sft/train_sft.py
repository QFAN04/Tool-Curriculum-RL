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


SPECIAL_TOKEN_INIT = {
    "<|im_start|>": "<| start message |>",
    "<|im_end|>": "<| end message |>",
    "<tool_call>": "<tool_call_begin>",
    "</tool_call>": "</tool_call_end>",
    "<think>": "<think_begin>",
    "</think>": "</think_end>",
}


def init_special_embeddings(model, tokenizer) -> list[int]:
    """用 Base 词表里语义相近的子词均值初始化未训练特殊 token（避免它们在 tied embedding 中彼此重合）。"""
    import torch

    special_ids = [tokenizer.convert_tokens_to_ids(t) for t in SPECIAL_TOKEN_INIT]
    w = model.get_input_embeddings().weight
    with torch.no_grad():
        # 若特殊 token 已彼此区分（如从 d3_sft_merged 继续做 E0+），保留原权重不重置
        if len(special_ids) >= 2 and (w[special_ids].float() - w[special_ids[0]].float()).abs().max().item() > 1e-3:
            return special_ids
        n_normal = min(150000, max(1, w.size(0) - len(special_ids)))
        target_norm = w[:n_normal].float().norm(dim=1).median().item()
        for phrase, tid in zip(SPECIAL_TOKEN_INIT.values(), special_ids):
            sub_ids = tokenizer.encode(phrase, add_special_tokens=False)
            v = w[sub_ids].float().mean(dim=0)
            v = v * (target_norm / (v.norm() + 1e-12))
            w[tid] = v.to(dtype=w.dtype, device=w.device)
        if not getattr(model.config, "tie_word_embeddings", True) and model.get_output_embeddings() is not None:
            ow = model.get_output_embeddings().weight
            ow[special_ids] = w[special_ids].to(dtype=ow.dtype, device=ow.device)
    return special_ids


def enable_special_token_grads(model, special_ids: list[int], init_weight=None):
    """只为 special_ids 这几行开启梯度更新，其余词表行梯度置零以保留未见工具的泛化表征。"""
    import torch

    w = model.get_input_embeddings().weight
    sp_ids = torch.tensor(special_ids, device=w.device, dtype=torch.long)
    base_rows = init_weight.detach().clone().to(device=w.device, dtype=torch.float32) if init_weight is not None else w.detach()[sp_ids].clone().float()
    sp_param = torch.nn.Parameter(base_rows)
    if init_weight is not None:
        with torch.no_grad():
            w[sp_ids] = sp_param.to(dtype=w.dtype, device=w.device)
    w.requires_grad_(True)

    def _hook(grad):
        g = grad[sp_ids].float()
        sp_param.grad = g if sp_param.grad is None else (sp_param.grad + g)
        return torch.zeros_like(grad)

    w.register_hook(_hook)
    return sp_param


def sync_special_rows(model, special_ids: list[int], sp_param) -> None:
    import torch

    w = model.get_input_embeddings().weight
    sp_ids = torch.tensor(special_ids, device=w.device, dtype=torch.long)
    with torch.no_grad():
        w[sp_ids] = sp_param.to(dtype=w.dtype, device=w.device)


def build_example(tokenizer, messages: list[dict], max_len: int):
    """返回 (input_ids, labels)。提示部分用模板渲染到「该 assistant 开口」为止，答案部分单独编码后接上结束符。"""
    from tcrl.prompts import CHAT_KWARGS

    prompt = tokenizer.apply_chat_template(messages[:-1], tokenize=False, add_generation_prompt=True, **CHAT_KWARGS)
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
    sp_path = os.path.join(args.merge, "special_tokens.pt")
    if os.path.exists(sp_path):
        sp = torch.load(sp_path, map_location="cpu", weights_only=True)
        with torch.no_grad():
            w = base.get_input_embeddings().weight
            sp_ids = torch.tensor(sp["ids"], dtype=torch.long)
            w[sp_ids] = sp["weight"].to(dtype=w.dtype, device=w.device)
            if not getattr(base.config, "tie_word_embeddings", True) and base.get_output_embeddings() is not None:
                ow = base.get_output_embeddings().weight
                ow[sp_ids] = sp["weight"].to(dtype=ow.dtype, device=ow.device)
    model = PeftModel.from_pretrained(base, args.merge).merge_and_unload()
    # Base 模型的 eos 是 <|endoftext|>，而 SFT 教的是以 <|im_end|> 结束一轮。
    # 把两者都设为停止符，并把 tokenizer 的 eos 改成 <|im_end|>（与 Instruct 版一致），
    # 这样 vLLM 和 verl 都会在 <|im_end|> 处停。
    tok = AutoTokenizer.from_pretrained(args.model)
    im_end, eot = tok.convert_tokens_to_ids("<|im_end|>"), tok.convert_tokens_to_ids("<|endoftext|>")
    model.generation_config.eos_token_id = [im_end, eot]
    model.config.eos_token_id = im_end
    model.to(torch.bfloat16).save_pretrained(args.out, safe_serialization=True)
    tok.eos_token = "<|im_end|>"
    tok.pad_token = "<|endoftext|>"
    tok.save_pretrained(args.out)
    print("merged ->", args.out)


def restart_tail_schedule(optimizer, lr: float):
    """Resume optimizer moments at a fixed positive LR after cosine decay."""
    from torch.optim.lr_scheduler import LambdaLR

    for group in optimizer.param_groups:
        group["lr"] = group["initial_lr"] = lr
    return LambdaLR(optimizer, lambda _: 1.0)



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
    ap.add_argument("--tail-lr", type=float, help="Resume with a constant positive LR, preserving optimizer moments")
    args = ap.parse_args()
    if args.tail_lr is not None and (not args.resume or args.tail_lr <= 0 or args.max_steps <= 0):
        ap.error("--tail-lr requires --resume, a positive learning rate and explicit --max-steps")
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
    special_ids = init_special_embeddings(model, tok)
    start, ckpts = 0, sorted(
        (
            d for d in os.listdir(args.out)
            if d.startswith("step-")
            and os.path.exists(os.path.join(args.out, d, "trainer_state.pt"))
            and os.path.exists(os.path.join(args.out, d, "special_tokens.pt"))
        ),
        key=lambda d: int(d.split("-")[1]),
    )
    if args.tail_lr is not None and (not ckpts or int(ckpts[-1].split("-")[1]) >= total):
        ap.error("--tail-lr requires a complete checkpoint before --max-steps")
    resume_sp_weight = None
    if args.resume and ckpts:
        sp = torch.load(os.path.join(args.out, ckpts[-1], "special_tokens.pt"), map_location="cpu", weights_only=True)
        resume_sp_weight = sp["weight"]
        model = PeftModel.from_pretrained(model, os.path.join(args.out, ckpts[-1]), is_trainable=True)
        start = int(ckpts[-1].split("-")[1])
    else:
        model = get_peft_model(
            model,
            LoraConfig(task_type="CAUSAL_LM", r=args.lora_rank, lora_alpha=args.lora_alpha, target_modules="all-linear"),
        )
    sp_param = enable_special_token_grads(model, special_ids, init_weight=resume_sp_weight)
    w_emb = model.get_input_embeddings().weight
    params = [p for p in model.parameters() if p.requires_grad and p is not w_emb] + [sp_param]
    opt = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.0)
    sched = get_cosine_schedule_with_warmup(opt, int(0.03 * total), total)
    state = os.path.join(args.out, ckpts[-1], "trainer_state.pt") if (args.resume and ckpts) else None
    if state and os.path.exists(state):
        st = torch.load(state)
        opt.load_state_dict(st["opt"])
        if args.tail_lr is None:
            sched.load_state_dict(st["sched"])
        else:
            sched = restart_tail_schedule(opt, args.tail_lr)

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
            w_emb.grad = None
            loss_sum += loss.item()
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        opt.step()
        sync_special_rows(model, special_ids, sp_param)
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
            torch.save({"ids": special_ids, "weight": sp_param.detach().cpu()}, os.path.join(d, "special_tokens.pt"))
    final_dir = os.path.join(args.out, "final")
    model.save_pretrained(final_dir)
    torch.save({"ids": special_ids, "weight": sp_param.detach().cpu()}, os.path.join(final_dir, "special_tokens.pt"))
    print("done")


if __name__ == "__main__":
    main()
