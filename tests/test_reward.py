"""奖励的正例与反例。反例是审查时要求的：错参数、漏调用、多调用、类型错、该拒绝时乱调用。"""
import json

from tcrl.reward import calls_match, compute_score
from tcrl.tool_env import parse_tool_calls, validate_call, validation_feedback

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "days": {"type": "integer"},
                    "unit": {"type": "string", "enum": ["c", "f"]},
                },
                "required": ["city"],
            },
        },
    },
    {"name": "send_email", "parameters": {"type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"]}},
]


def tc(name, **args):
    return "<tool_call>\n" + json.dumps({"name": name, "arguments": args}) + "\n</tool_call>"


GOLD = [{"name": "get_weather", "arguments": {"city": "Paris", "days": 3}}]


def score(text, gold=GOLD, **kw):
    return compute_score(text, gold, TOOLS, **kw)["score"]


# ---------- 正例 ----------
def test_exact_match():
    assert score(tc("get_weather", city="Paris", days=3)) == 1.0


def test_argument_order_and_whitespace_do_not_matter():
    assert score(tc("get_weather", days=3, city=" Paris ")) == 1.0


def test_integer_valued_float_equals_int():
    assert score(tc("get_weather", city="Paris", days=3.0)) == 1.0


def test_text_around_call_is_ignored():
    assert score("Let me check.\n" + tc("get_weather", city="Paris", days=3) + "\nDone.") == 1.0


def test_parallel_calls_order_free():
    gold = GOLD + [{"name": "send_email", "arguments": {"to": "a@b.c"}}]
    assert score(tc("send_email", to="a@b.c") + tc("get_weather", city="Paris", days=3), gold) == 1.0


def test_refusal_correct_when_no_call_expected():
    assert score("I cannot help with that using the available tools.", gold=[]) == 1.0


def test_arguments_given_as_json_string():
    text = '<tool_call>{"name": "get_weather", "arguments": "{\\"city\\": \\"Paris\\", \\"days\\": 3}"}</tool_call>'
    assert score(text) == 1.0


# ---------- 反例 ----------
def test_wrong_argument_value():
    assert score(tc("get_weather", city="London", days=3)) == 0.0


def test_missing_argument():
    assert score(tc("get_weather", city="Paris")) == 0.0


def test_extra_argument():
    assert score(tc("get_weather", city="Paris", days=3, unit="c")) == 0.0


def test_wrong_function():
    assert score(tc("send_email", to="Paris")) == 0.0


def test_missing_call():
    gold = GOLD + [{"name": "send_email", "arguments": {"to": "a@b.c"}}]
    assert score(tc("get_weather", city="Paris", days=3), gold) == 0.0


def test_extra_call():
    assert score(tc("get_weather", city="Paris", days=3) + tc("send_email", to="a@b.c")) == 0.0


def test_duplicate_call_counts():
    # 多重集：重复调用一次不等于调用一次
    assert score(tc("get_weather", city="Paris", days=3) * 2) == 0.0
    assert score(tc("get_weather", city="Paris", days=3) * 2, GOLD * 2) == 1.0


def test_string_is_not_number():
    assert score(tc("get_weather", city="Paris", days="3")) == 0.0


def test_bool_is_not_int():
    gold = [{"name": "get_weather", "arguments": {"city": "Paris", "days": 1}}]
    assert score(tc("get_weather", city="Paris", days=True), gold) == 0.0


def test_case_sensitive_strings():
    assert score(tc("get_weather", city="paris", days=3)) == 0.0


def test_call_when_refusal_expected():
    assert score(tc("get_weather", city="Paris", days=3), gold=[]) == 0.0


def test_no_call_when_call_expected():
    assert score("The weather in Paris is sunny.") == 0.0


def test_malformed_json_is_zero_and_flagged():
    out = compute_score('<tool_call>{"name": "get_weather", "arguments": {"city": "Paris", </tool_call>', GOLD, TOOLS)
    assert out["score"] == 0.0 and out["incorrect_format"] == 1


def test_unclosed_tag_is_format_error_even_if_other_call_is_right():
    text = tc("get_weather", city="Paris", days=3) + '<tool_call>{"name": "send_email"'
    out = compute_score(text, GOLD, TOOLS)
    assert out["score"] == 0.0 and out["incorrect_format"] == 1


def test_list_order_matters():
    gold = [{"name": "f", "arguments": {"xs": [1, 2]}}]
    assert calls_match([{"name": "f", "arguments": {"xs": [1, 2]}}], gold)
    assert not calls_match([{"name": "f", "arguments": {"xs": [2, 1]}}], gold)


# ---------- 校验与反馈 ----------
def test_validate_unknown_function():
    assert validate_call({"name": "nope", "arguments": {}}, TOOLS) == ["unknown function 'nope'"]


def test_validate_missing_required_type_enum_unexpected():
    errs = validate_call({"name": "get_weather", "arguments": {"days": "3", "unit": "k", "zip": 1}}, TOOLS)
    joined = " | ".join(errs)
    assert "missing required parameter 'city'" in joined
    assert "'days' should be of type integer" in joined
    assert "'unit' must be one of" in joined
    assert "unexpected parameter 'zip'" in joined


def test_validation_feedback_empty_when_schema_valid():
    assert validation_feedback(tc("get_weather", city="London"), TOOLS) == ""


def test_feedback_hides_gold_by_default_and_shows_it_on_request():
    wrong = tc("get_weather", city="London", days=3)
    hidden = compute_score(wrong, GOLD, TOOLS)["feedback"]
    shown = compute_score(wrong, GOLD, TOOLS, include_gold_in_feedback=True)["feedback"]
    assert "Paris" not in hidden and "argument values differ" in hidden
    assert "Paris" in shown


def test_feedback_empty_when_correct():
    assert compute_score(tc("get_weather", city="Paris", days=3), GOLD, TOOLS)["feedback"] == ""


def test_parse_reports_each_bad_block():
    r = parse_tool_calls("<tool_call>[1,2]</tool_call><tool_call>{\"arguments\": {}}</tool_call>")
    assert len(r.errors) == 2 and r.calls == []
