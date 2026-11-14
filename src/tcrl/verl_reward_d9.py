"""D9 rewards keep tool scoring independent of the generated thinking block."""
from tcrl.thinking import split_thinking


def compute_score(data_source, solution_str, ground_truth, extra_info=None, *, graded=False, thinking=False,
                  prefixed=False):
    if graded:
        from tcrl.verl_reward_graded import compute_score as score
    else:
        from tcrl.verl_reward import compute_score as score
    if not thinking:
        return score(data_source, solution_str, ground_truth, extra_info)
    _, final = split_thinking(solution_str, prefixed=prefixed)
    result = score(data_source, final or "", ground_truth, extra_info)
    if not final or (extra_info or {}).get("truncated", False):
        result.update(score=0.0, acc=0.0, incorrect_format=1)
    return result
