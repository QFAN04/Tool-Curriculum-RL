import os
from pathlib import Path

import pytest

from tcrl.curriculum.probe_strata import summarize
from tcrl.eval.bfcl import CATEGORIES, dataset, decode, json_schema, messages_for, score
from tcrl.prompts import render_calls


def test_nested_schema_and_call_preserve_names_and_values():
    schema = {"type": "tuple", "items": {"type": "dict", "properties": {"type": {"type": "string"}}}}
    assert json_schema(schema) == {
        "type": "array", "items": {"type": "object", "properties": {"type": {"type": "string"}}}
    }
    assert schema["type"] == "tuple"
    calls = [{"name": "stats.group_by", "arguments": {"keys": ["a"], "opts": {"x": True, "y": None}}}]
    assert decode(render_calls(calls)) == [{"stats.group_by": calls[0]["arguments"]}]
    assert decode("<tool_call>{") is None
    assert decode("</tool_call>") is None
    assert score("irrelevance", {}, None, "No available tool applies.")
    assert not score("irrelevance", {}, None, render_calls(calls))
    assert not score("irrelevance", {}, None, "No available tool applies.", truncated=True)


def test_probe_splits_use_union_of_seed_ids_for_both_arms():
    rows = [{"id": "a", "scores": [1] * 8}, {"id": "b", "scores": [0] * 8},
            {"id": "c", "scores": [1, 0] * 4}]
    ids = {"a", "b", "c"}
    seeds = {"cu": {"a"}, "cf": {"a", "b"}}
    result = summarize(rows, ids, seeds)
    assert result["all"]["mixed_fraction"] == 1 / 3
    assert result["non_seed"]["mixed_fraction"] == 1
    assert result["any_seed"]["n"] == 2
    with pytest.raises(ValueError):
        summarize(rows[:-1], ids, seeds)


@pytest.mark.skipif("TCRL_BFCL_TEST_DATA" not in os.environ, reason="requires frozen BFCL data and official package")
def test_official_bfcl_scorer_on_real_dataset():
    examples = list(dataset(Path(os.environ["TCRL_BFCL_TEST_DATA"])))
    assert len(examples) == 1240
    checked = set()
    for category, row, gold in examples:
        messages, _ = messages_for(row)
        assert messages[0]["role"] == "system"
        if category in checked:
            continue
        checked.add(category)
        if gold is None:
            assert score(category, row, gold, "No tool applies.")
            continue
        calls = [
            {"name": name, "arguments": {key: next(value for value in values if value != "")
                                         for key, values in params.items() if any(value != "" for value in values)}}
            for call in gold for name, params in call.items()
        ]
        assert score(category, row, gold, render_calls(calls)), row["id"]
        if len(calls) > 1:
            assert score(category, row, gold, render_calls(calls[::-1])), row["id"]
        assert not score(category, row, gold, render_calls(calls), truncated=True)
        assert not score(category, row, gold, "No tool applies.")
        calls[0]["arguments"]["tcrl_invalid"] = True
        assert not score(category, row, gold, render_calls(calls)), row["id"]
    assert checked == set(CATEGORIES)
