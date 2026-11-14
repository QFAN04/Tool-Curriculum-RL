"""Shared thinking protocol for inference, SFT and D9 rewards."""
OPEN, CLOSE = "<think>", "</think>"


def split_thinking(text, prefixed=False):
    """Return (plan, final); a missing or malformed block has no final."""
    text = text.strip()
    if text.startswith(OPEN):
        text = text[len(OPEN):]
    elif not prefixed:
        return "", None
    plan, separator, final = text.partition(CLOSE)
    if not separator or OPEN in plan or OPEN in final or CLOSE in final:
        return plan.strip(), None
    return plan.strip(), final.strip()


def thinking_stats(completions):
    n = len(completions)
    return {
        "nonempty_thinking_fraction": sum(x["nonempty"] for x in completions) / n,
        "closed_thinking_fraction": sum(x["closed"] for x in completions) / n,
        "nonempty_closed_fraction": sum(x["nonempty"] and x["closed"] for x in completions) / n,
        "mean_thinking_tokens": sum(x["tokens"] for x in completions) / n,
    }


def unpack_completion(text, tokenizer, prompt, truncated):
    for stop in ("<|im_end|>", "<|endoftext|>"):
        text = text.rstrip().removesuffix(stop)
    plan, final = split_thinking(text, prefixed=prompt.rstrip().endswith(OPEN))
    metrics = {"nonempty": bool(plan), "closed": final is not None,
               "tokens": len(tokenizer.encode(plan, add_special_tokens=False)),
               "truncated": truncated}
    return (final if final and not truncated else None), metrics


def thinking_target(text, prompt):
    if not text.startswith(OPEN):
        raise ValueError("Thinking SFT requires an explicitly tagged plan")
    plan, final = split_thinking(text)
    if final is None or not final:
        raise ValueError("Thinking SFT target must contain a closed plan and final answer")
    return text[len(OPEN):] if prompt.rstrip().endswith(OPEN) else text
