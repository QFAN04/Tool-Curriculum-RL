import json

import pytest

from tcrl.curriculum.replay import extra_steps, replay_sets
from tcrl.prompts import render_calls
from tcrl.sft.train_sft import restart_tail_schedule
from tcrl.verl_reward_graded import compute_score


TOOLS = [
    {"name": name, "parameters": {
        "properties": {"value": {"type": "integer"}, "flag": {"type": "boolean", "default": False}},
        "required": ["value"],
    }} for name in ("a", "b", "other")
]


def call(name, value, **kwargs):
    return {"name": name, "arguments": {"value": value, **kwargs}}


def grade(pred, gold, *, raw=None):
    return compute_score("tcrl", render_calls(pred) if raw is None else raw, json.dumps(gold),
                         {"tools_json": json.dumps(TOOLS)})


def test_x2_exact_and_default_equivalence_keep_strict_accuracy_separate():
    explicit = [call("a", 1, flag=False)]
    implicit = [call("a", 1)]
    assert grade(explicit, explicit)["score"] == 1
    for pred, gold in ((explicit, implicit), (implicit, explicit)):
        result = grade(pred, gold)
        assert result["score"] == 1
        assert result["acc"] == 0
    assert grade([call("a", 1, flag=True)], implicit)["score"] == pytest.approx(0.4)
    assert grade([call("a", 1, flag=0)], implicit)["score"] == pytest.approx(0.3)


def test_x2_credits_partial_multisets_and_penalizes_duplicate_calls():
    gold = [call("a", 1), call("b", 2)]
    assert grade(gold[::-1], gold)["score"] == 1
    assert grade(gold[:1], gold)["score"] == pytest.approx(2 / 3)
    assert grade([gold[0], gold[0]], gold)["score"] == pytest.approx(0.5)
    assert grade([call("a", 9), call("b", 9)], gold)["score"] == pytest.approx(0.4)
    assert grade([call("other", 1)], gold)["score"] == 0
    assert grade([], gold)["score"] == 0
    assert grade(gold, [])["score"] == 0
    assert grade([], [], raw="No tool applies.")["score"] == 1
    assert grade([], gold, raw="<tool_call>{")["score"] == 0
    assert grade([], [], raw="</tool_call>")["score"] == 0
    assert grade([], gold, raw="I will call it.\n" + render_calls(gold))["score"] == 0


def test_replay_uses_same_refusals_and_preserves_calls():
    arms = {arm: [{"id": f"{arm}{i}"} for i in range(300)] for arm in ("cs", "cu", "cf")}
    refusals = [
        {"kind": "refuse", "id": str(i), "question": "Hello", "tools": [], "refusal": "Hi"}
        for i in range(60)
    ]
    output, ids = replay_sets(arms, refusals)
    assert len(set(ids)) == 53
    assert replay_sets(arms, list(reversed(refusals))) == (output, ids)
    for arm, rows in output.items():
        assert rows[:300] == arms[arm]
        assert [row["id"] for row in rows[300:]] == ids
    assert extra_steps([{"step": 20, "elapsed_s": 220}], 95) == 8


def test_tail_schedule_restores_positive_lr_without_resetting_adam_moments():
    torch = pytest.importorskip("torch")
    param = torch.nn.Parameter(torch.tensor(1.0))
    opt = torch.optim.AdamW([param], lr=2e-5)
    param.grad = torch.ones_like(param)
    opt.step()
    state = opt.state_dict()
    state["param_groups"][0]["lr"] = 0.0
    state["param_groups"][0]["initial_lr"] = 2e-5
    resumed = torch.optim.AdamW([param], lr=2e-5)
    resumed.load_state_dict(state)
    tail = restart_tail_schedule(resumed, 1e-5)
    assert resumed.param_groups[0]["lr"] == 1e-5
    assert resumed.state[param]["step"].item() == 1
    assert torch.equal(resumed.state[param]["exp_avg"], opt.state[param]["exp_avg"])
    before = param.item()
    resumed.step()
    tail.step()
    assert resumed.param_groups[0]["lr"] == 1e-5
    assert param.item() < before
