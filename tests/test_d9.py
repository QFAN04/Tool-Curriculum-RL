import json
from collections import Counter

import pytest

from tcrl.data.d9 import native_plan, paired_raw, synthetic
from tcrl.data.split import visible_families
from tcrl.eval.d9 import call_mixed_fraction, graded_signal, paired, thinking_pass
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
    coverage = {}
    for row in rows:
        assert row["question"] != row["literal_question"] and len(row["gold"]) == 3
        assert len(row["tools"]) == 8
        assert len({call["name"] for call in row["gold"]}) == 3
        assert all("encoding" not in tool["name"] for tool in row["tools"])
        assert "lookup" in row["gold"][0]["name"]
        if row["partition"] == "heldout":
            for call in row["gold"]:
                kind = call["name"].split("_set_", 1)[-1]
                coverage.setdefault(kind, set()).update(call["arguments"].values())
    assert coverage["boolean"] == {True, False}
    assert coverage["top_k"] == set(range(1, 11))
    assert len({row["conversion"] for row in rows}) == 21
    for row in rows:
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


def test_three_calls_create_graded_signal_on_strict_all_wrong_group():
    row = next(synthetic())
    gold = row["gold"]
    wrong = [
        {**call, "arguments": {key: not value if type(value) is bool else value + 1
                               for key, value in call["arguments"].items()}}
        for call in gold[1:]
    ]
    expected = json.dumps(gold)
    info = {"tools_json": json.dumps(row["tools"])}
    predictions = [[gold[0], *wrong], [*gold[:2], wrong[1]]] * 4
    outputs = ["<think>Plan.</think>" + render_calls(calls) for calls in predictions]
    scores = [compute_score("tcrl", output, expected, info, thinking=True)["score"] for output in outputs]
    grades = [compute_score("tcrl", output, expected, info, thinking=True, graded=True)["score"]
              for output in outputs]
    assert scores == [0] * 8
    assert grades == pytest.approx([.60, .80] * 4)
    [detail] = [{"kind": "call", "scores": scores, "graded_scores": grades}]
    assert call_mixed_fraction([detail]) == 0
    assert graded_signal([detail])["graded_signal_on_binary_all_wrong"] == 1



def test_direct_answer_rewards_measure_partial_calls_without_think():
    row = next(synthetic())
    gold = row["gold"]
    expected = json.dumps(gold)
    info = {"tools_json": json.dumps(row["tools"])}
    strict = [compute_score("tcrl", render_calls(gold[:n]), expected, info)["score"] for n in (1, 2, 3)]
    graded = [compute_score("tcrl", render_calls(gold[:n]), expected, info, graded=True)["score"]
              for n in (1, 2, 3)]
    assert strict == [0, 0, 1]
    assert graded == pytest.approx([0.50, 0.80, 1.0])
    rows = [{"kind": "call", "scores": [0] * 8, "graded_scores": graded[:2] * 4}]
    assert graded_signal(rows)["binary_all_wrong_groups"] == 1
    assert graded_signal(rows)["graded_signal_on_binary_all_wrong"] == 1


def test_d9_cli_accepts_output_after_subcommand(tmp_path, monkeypatch):
    from tcrl.eval.d9 import main

    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({
        "n_prompts": 20, "n": 1, "truncated_fraction": 0,
        "thinking": {"nonempty_closed_fraction": 0, "closed_thinking_fraction": 0},
    }), encoding="utf8")
    out = tmp_path / "gate.json"
    monkeypatch.setattr("sys.argv", ["d9", "thinking", str(summary), "--out", str(out)])
    main()
    assert json.loads(out.read_text())["pass"] is False

    pairs, samples = tmp_path / "pairs.jsonl", tmp_path / "samples.jsonl"
    pairs.write_text('{"id":"p","conversion":"unit"}\n', encoding="utf8")
    samples.write_text('{"id":"p:literal","scores":[1]}\n{"id":"p:conversion","scores":[0]}\n', encoding="utf8")
    monkeypatch.setattr("sys.argv", ["d9", "paired", "--samples", str(samples), "--pairs", str(pairs),
                                    "--out", str(out)])
    main()
    assert json.loads(out.read_text())["all"]["gap"] == 1
