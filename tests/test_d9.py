import json
from collections import Counter

import pytest

from tcrl.data.d9 import native_plan, paired_raw, synthetic
from tcrl.data.split import visible_families
from tcrl.eval.d9 import call_mixed_fraction, paired, thinking_pass
from tcrl.prompts import render_calls
from tcrl.sft.train_sft import build_example
from tcrl.thinking import split_thinking, unpack_completion
from tcrl.tool_env import validate_call
from tcrl.verl_reward_d9 import compute_score


class FakeTokenizer:
    def apply_chat_template(self, messages, *, enable_thinking, **kwargs):
        return "prompt<think>\n" if enable_thinking else "prompt<think>\n\n</think>\n\n"

    def encode(self, text, **kwargs):
        return list(map(ord, text))

    def __call__(self, text, **kwargs):
        return {"input_ids": self.encode(text)}


def test_think_scoring_ignores_calls_in_plan_and_rejects_broken_output():
    call = {"name": "calc", "arguments": {"rate": 0.05}}
    gold = json.dumps([call])
    final = render_calls([call])
    raw = f"<think>{render_calls([{'name': 'wrong', 'arguments': {}}])}</think>{final}"
    assert split_thinking(raw)[1] == final
    for graded in (False, True):
        assert compute_score("tcrl", raw, gold, thinking=True, graded=graded)["score"] == 1
        assert compute_score("tcrl", "<think>" + final, "[]", thinking=True, graded=graded)["score"] == 0
    text, stats = unpack_completion("plan</think>" + final, FakeTokenizer(), "prompt<think>\n", False)
    assert text == final and stats == {"nonempty": True, "closed": True, "tokens": 4, "truncated": False}
    assert unpack_completion(raw, FakeTokenizer(), "prompt", True)[0] is None
    assert split_thinking("<think>nested<think></think>answer")[1] is None
    assert compute_score("tcrl", "<think>No relevant tool.</think>I can answer in text.", "[]", thinking=True)["score"] == 1
    assert compute_score("tcrl", "<think></think>partial", "[]", {"truncated": True}, thinking=True)["score"] == 0


def test_sft_supervises_plan_and_close_exactly_once():
    messages = [{"role": "user", "content": "Ask"},
                {"role": "assistant", "content": "<think>Plan</think>Answer"}]
    ids, labels = build_example(FakeTokenizer(), messages, 100, enable_thinking=True)
    assert "".join(map(chr, ids)).count("<think>") == 1
    assert "".join(chr(t) for t in labels if t != -100) == "Plan</think>Answer<|im_end|>"
    assert build_example(FakeTokenizer(), messages, 20, enable_thinking=True) == ([], [])
    messages[-1]["content"] = "Answer"
    _, labels = build_example(FakeTokenizer(), messages, 100)
    assert "".join(chr(t) for t in labels if t != -100) == "Answer<|im_end|>"


def test_raw_pair_uses_exact_conversion_and_only_pre_call_planning():
    source = {"messages": json.dumps([
        {"role": "user", "content": "Set 5%"},
        {"role": "assistant", "content": "Use a fraction."},
        {"role": "tool_call", "content": "{}"},
        {"role": "assistant", "content": "Already done."},
    ])}
    assert native_plan(source) == "Use a fraction."
    raw = {"question": "Set the ratio to 5%.", "gold": [{"name": "calc", "arguments": {"ratio": .05}}],
           "tools": [{"name": "calc", "parameters": {"properties": {"ratio": {"type": "number",
                     "description": "Decimal fraction"}}, "required": ["ratio"]}}]}
    pair = paired_raw(raw)
    assert pair["gold"] == raw["gold"]
    assert "0.05" in pair["literal_question"]
    assert paired_raw({**raw, "question": "Set the ratio to 7%."}) is None


def test_generated_gold_valid_and_visible_families_isolated():
    partitions = {k: set() for k in ("train", "gate", "heldout")}
    rows = list(synthetic())
    assert Counter(row["partition"] for row in rows) == {"train": 588, "gate": 196, "heldout": 392}
    for row in rows:
        assert row["question"] != row["literal_question"] and len(row["gold"]) == 2
        assert all(not validate_call(call, row["tools"]) for call in row["gold"])
        partitions[row["partition"]].update(visible_families(row))
    assert sum(map(len, partitions.values())) == len(set().union(*partitions.values()))


def test_d9_gates_and_pair_integrity():
    p = {"id": "pair", "conversion": "unit"}
    rows = [{"id": "pair:literal", "scores": [1]}, {"id": "pair:conversion", "scores": [0]}]
    assert paired(rows, [p])["all"]["gap"] == 1
    with pytest.raises(ValueError):
        paired(rows + rows, [p])
    assert call_mixed_fraction([{"kind": "call", "scores": [0, 1] * 4},
                                {"kind": "refuse", "scores": [1] * 8}]) == 1
    summary = {"n_prompts": 20, "n": 1, "truncated_fraction": .1,
               "thinking": {"nonempty_closed_fraction": .8, "closed_thinking_fraction": .9}}
    assert thinking_pass(summary)
    assert not thinking_pass({**summary, "truncated_fraction": .15})
