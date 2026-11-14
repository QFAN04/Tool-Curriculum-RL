"""X2-only verl reward. Strict accuracy stays available as a separate metric."""
from collections import Counter
import json

from tcrl.curriculum.seeds import strip_optional_defaults
from tcrl.reward import compute_score as strict_score
from tcrl.tool_env import TOOL_CALL_RE, canonical, parse_tool_calls, validate_call


def multiset_f1(pred, gold):
    size = sum(pred.values()) + sum(gold.values())
    return 2 * sum((pred & gold).values()) / size if size else 1.0


def compute_score(data_source, solution_str, ground_truth, extra_info=None):
    tools = json.loads((extra_info or {}).get("tools_json") or "[]")
    gold = json.loads(ground_truth) if isinstance(ground_truth, str) else ground_truth
    result = strict_score(solution_str, gold, tools)
    parsed = parse_tool_calls(solution_str)
    calls = parsed.calls
    if (not parsed.ok or solution_str.count("</tool_call>") != len(calls)
            or (calls and TOOL_CALL_RE.sub("", solution_str).strip())):
        score = 0.0
    elif result["acc"] == 1:
        score = 1.0
    elif not gold or not calls:
        score = 0.0
    else:
        pred = strip_optional_defaults(calls, tools)
        expected = strip_optional_defaults(gold, tools)
        names = multiset_f1(Counter(c["name"] for c in pred), Counter(c["name"] for c in expected))
        exact = multiset_f1(Counter(map(canonical, pred)), Counter(map(canonical, expected)))
        valid = all(not validate_call(call, tools) for call in calls)
        # A valid but unrelated call earns zero. Invalid calls cannot earn full credit.
        score = 0.1 * valid * names + 0.3 * names + 0.6 * exact
    result["score"] = score
    if extra_info and "id" in extra_info:
        result.update(sample_id=extra_info["id"], split=extra_info.get("split", ""))
    return result
