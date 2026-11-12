"""划分的隔离性：留出族不能以任何形式出现在训练侧。先用合成数据测逻辑，再（如果文件在）查真实划分。"""
import json
import os

import pytest

from tcrl.data.build import clean_question, grounded
from tcrl.data.split import check_isolation, gold_families, make_splits, tool_family, visible_families


def tool(name, *params):
    return {"name": name, "parameters": {"type": "object", "properties": {p: {"type": "string"} for p in params}}}


def rec(i, tools, gold_names, kind="call"):
    gold = [{"name": n, "arguments": {}} for n in gold_names]
    return {"id": f"r{i}", "kind": kind, "question": "q", "tools": tools, "gold": gold, "refusal": None, "subset": "s"}


def synthetic(n_fam=40, per_fam=30):
    recs, i = [], 0
    for f in range(n_fam):
        t = tool(f"srv{f}-do", f"p{f}")
        other = tool(f"srv{(f + 1) % n_fam}-do", f"p{(f + 1) % n_fam}")  # 每条样本还带一个别族的干扰工具
        for _ in range(per_fam):
            recs.append(rec(i, [t, other], [t["name"]]))
            i += 1
        recs.append(rec(i, [t], [], kind="refuse"))
        i += 1
    return recs


def test_family_ignores_server_prefix_and_function_spelling():
    a = tool("mcp-word-server-get_live_temp", "latitude", "longitude")
    b = tool("other-server-getLiveTemperature", "longitude", "latitude")
    assert tool_family(a) == tool_family(b)
    assert tool_family(tool("x-ping")) != tool_family(tool("x-health_check"))


def test_synthetic_split_is_isolated():
    s = make_splits(synthetic(), n_test_seen=50, n_dev_seen=20, max_family_frac=0.2)
    check_isolation(s)
    held = set(s["_meta"]["held_families"]["test_unseen"]) | set(s["_meta"]["held_families"]["dev_unseen"])
    assert held and s["test_unseen"] and s["sft"] and s["rl"]
    for name in ("sft", "rl", "test_seen", "dev_seen"):
        for r in s[name]:
            assert not (visible_families(r) & held)  # 连干扰工具里都不能有留出族


def test_check_isolation_catches_a_leak():
    s = make_splits(synthetic(), n_test_seen=50, n_dev_seen=20, max_family_frac=0.2)
    leaked = dict(next(r for r in s["test_unseen"] if r["kind"] == "call"))
    leaked["id"] = "leak"
    s["sft"].append(leaked)
    with pytest.raises(AssertionError):
        check_isolation(s)


def test_split_is_deterministic():
    a = make_splits(synthetic(), n_test_seen=50, n_dev_seen=20, max_family_frac=0.2)
    b = make_splits(synthetic(), n_test_seen=50, n_dev_seen=20, max_family_frac=0.2)
    assert [r["id"] for r in a["rl"]] == [r["id"] for r in b["rl"]]


def test_grounded_filter():
    props = {"unit": {"type": "string", "enum": ["c", "f"]}}
    assert grounded({"city": "New York", "days": 3}, "Weather in new york for 3 days?", {})
    assert not grounded({"city": "Boston"}, "Weather in New York?", {})
    assert not grounded({"days": 3}, "Weather for 30 days", {})  # 整词匹配，3 不等于 30
    # 必填的枚举 / 布尔可以不出现在问题里；可选的必须出现，否则填不填都行
    assert grounded({"unit": "c", "flag": True}, "anything", props, required=("unit", "flag"))
    assert not grounded({"unit": "c"}, "anything", props)
    assert not grounded({"flag": True}, "anything", props)
    assert grounded({"unit": "c"}, "give it in c please", props)


def test_long_free_text_must_be_quoted_verbatim():
    idea = "an app that helps insurance agents assess damages from photos"
    assert not grounded({"idea": idea}, f"I have {idea}, what sector is it?", {})
    assert grounded({"prompt": idea}, f'Generate an image of "{idea}" please', {})
    assert grounded({"city": "New York City"}, "weather in New York City", {})  # 短字符串不受影响


def test_grounded_handles_non_ascii():
    assert grounded({"school": "한양중학교"}, "한양중학교 의 급식?", {})
    assert not grounded({"date": "내일"}, "What is lunch tomorrow at 한양중학교?", {})


def test_clean_question_strips_or_unwraps_html_comments():
    assert clean_question("<!-- note -->\n  Real question?") == "Real question?"
    assert clean_question("<!-- Only inside? -->") == "Only inside?"
    assert clean_question("plain") == "plain"


REAL = "data/processed/splits"


@pytest.mark.skipif(not os.path.exists(f"{REAL}/sft.jsonl"), reason="real splits not built")
def test_real_splits_are_isolated():
    names = ["sft", "rl", "test_seen", "dev_seen", "test_unseen", "dev_unseen"]
    s = {n: [json.loads(x) for x in open(f"{REAL}/{n}.jsonl", encoding="utf-8")] for n in names}
    m = json.load(open("data/manifests/splits.json", encoding="utf-8"))
    s["_meta"] = {"held_families": m["held_families"]}
    check_isolation(s)
    train_gold_fams = {f for n in ("sft", "rl") for r in s[n] if r["kind"] == "call" for f in gold_families(r)}
    test_gold_fams = {f for r in s["test_unseen"] if r["kind"] == "call" for f in gold_families(r)}
    assert not (train_gold_fams & test_gold_fams)
