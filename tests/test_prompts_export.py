"""提示渲染与导出：答题目标能被奖励判为满分；verl 奖励入口两种反馈设置；反向样本的结构。"""
import json
import os

from tcrl import verl_reward
from tcrl.data.export import rl_rows, sft_rows
from tcrl.prompts import generate_messages, render_calls, render_tools, solve_messages, solve_target
from tcrl.reward import compute_score

TOOLS = [{"name": "get_weather", "description": "d", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
CALL = {"id": "a", "kind": "call", "question": "Weather in Paris?", "tools": TOOLS,
        "gold": [{"name": "get_weather", "arguments": {"city": "Paris"}}], "refusal": None}
REF = {"id": "b", "kind": "refuse", "question": "Tell me a joke", "tools": TOOLS, "gold": [], "refusal": "Sure, here is one."}


def test_sft_target_scores_one_under_reward():
    for r in (CALL, REF):
        assert compute_score(solve_target(r), r["gold"], r["tools"])["score"] == 1.0


def test_tools_rendering_is_deterministic_and_in_system_message():
    a = dict(TOOLS[0]); b = {k: a[k] for k in reversed(list(a))}
    assert render_tools([a]) == render_tools([b])
    msgs = solve_messages(CALL)
    assert [m["role"] for m in msgs] == ["system", "user"] and "get_weather" in msgs[0]["content"]
    assert msgs[1]["content"] == "Weather in Paris?"


def test_generate_messages_contain_calls_not_question():
    msgs = generate_messages(TOOLS, CALL["gold"])
    assert render_calls(CALL["gold"]) in msgs[1]["content"] and "Weather in Paris?" not in json.dumps(msgs)


def test_sft_rows_have_reverse_samples_only_from_call_records():
    rows = sft_rows([CALL, REF] * 50, reverse_frac=0.1)
    gen = [r for r in rows if r["task"] == "generate"]
    assert len(gen) == 10 and all(r["messages"][-1]["content"] == "Weather in Paris?" for r in gen)
    assert all(r["messages"][-1]["role"] == "assistant" for r in rows)


def test_rl_rows_and_verl_reward_entry(monkeypatch):
    row = rl_rows([CALL], "rl")[0]
    assert row["data_source"] == "tcrl" and row["prompt"] == solve_messages(CALL)
    good = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>'
    bad = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Rome"}}\n</tool_call>'
    gt, ei = row["reward_model"]["ground_truth"], row["extra_info"]
    assert verl_reward.compute_score("tcrl", good, gt, ei)["score"] == 1.0
    monkeypatch.setenv("TCRL_FEEDBACK_GOLD", "1")
    assert "Paris" in verl_reward.compute_score("tcrl", bad, gt, ei)["feedback"]
    monkeypatch.setenv("TCRL_FEEDBACK_GOLD", "0")
    fb = verl_reward.compute_score("tcrl", bad, gt, ei)["feedback"]
    assert "Paris" not in fb and fb
