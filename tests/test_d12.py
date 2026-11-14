import csv
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from tcrl.eval import d12, run_eval


def test_frozen_d12_matrix():
    entries = d12.read_models(Path("scripts/autodl/d12_models.tsv"))
    assert {r["id"] for r in entries if r["n8"] == "1"} == {
        "b1", "e0", "e0plus", "e1", "e2b_gold_only", "x2", "d7_cf",
        "d8_cs", "d8_cu", "d8_cf", "d8_csplus", "d9_sft", "d9_binary", "d9_graded",
    }


def test_dual_decode_uses_one_engine_and_fixed_sampling(monkeypatch, tmp_path):
    engines = []
    settings = []

    class FakeEngine:
        def __init__(self, **kwargs):
            engines.append(self)

        def generate(self, prompts, params):
            settings.append(params)
            completion = SimpleNamespace(text="No suitable tool", token_ids=[1], finish_reason="stop")
            return [SimpleNamespace(outputs=[completion] * params["n"]) for _ in prompts]

    tokenizer = SimpleNamespace(apply_chat_template=lambda *args, **kwargs: "prompt",
                                convert_tokens_to_ids=lambda token: 1)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=lambda model: tokenizer)))
    monkeypatch.setitem(sys.modules, "vllm", SimpleNamespace(LLM=FakeEngine, SamplingParams=dict))
    row = {"prompt": [{"role": "system", "content": "solve"}, {"role": "user", "content": "mock"}],
           "extra_info": {"id": "sample", "split": "test_seen", "kind": "refuse", "n_calls": 0,
                          "tools_json": "[]"},
           "reward_model": {"ground_truth": "[]"}}
    data = tmp_path / "mock.parquet"
    pq.write_table(pa.Table.from_pylist([row]), data)
    greedy, sample8 = tmp_path / "greedy", tmp_path / "sample8"
    monkeypatch.setattr(sys, "argv", ["run_eval", "--model", "mock", "--data", str(data),
                                     "--out", str(greedy), "--also-sample-eight-to", str(sample8)])
    run_eval.main()
    assert len(engines) == 1
    assert [tuple(s[k] for k in ("n", "temperature", "top_p", "top_k", "max_tokens"))
            for s in settings] == [(1, 0, 1, -1, 1024), (8, 1, 1, -1, 512)]
    assert json.loads((sample8 / "samples.jsonl").read_text())["scores"] == [1.0] * 8


def test_symmetric_optional_defaults_preserve_calls_and_required_values():
    tool = {"name": "lookup", "parameters": {
        "required": ["id"], "properties": {"id": {"default": 0}, "flag": {"default": False}},
    }}
    gold = [{"name": "lookup", "arguments": {"id": 0, "flag": False}}]

    def call(arguments):
        return "<tool_call>" + json.dumps({"name": "lookup", "arguments": arguments}) + "</tool_call>"

    omitted = call({"id": 0})
    explicit = call({"id": 0, "flag": False})
    assert d12.score([omitted, explicit, call({"flag": False}), call({"id": 0, "flag": 0}),
                      explicit * 2, "<tool_call>"], gold, [tool]) == (
        [0, 1, 0, 0, 0, 0], [1, 1, 0, 0, 0, 0])
    assert d12.score([explicit], [{"name": "lookup", "arguments": {"id": 0}}], [tool]) == ([0], [1])
    assert d12.score(["No suitable tool", "<tool_call>"], [], []) == ([1, 0], [1, 0])


def test_cluster_bootstrap_pairs_rows_and_weights_cluster_sizes():
    values = np.array([[[1], [0]], [[1], [0]], [[0], [1]]])
    _, delta, stats, count = d12.cluster_statistics(
        values, ["weather", "weather", "search"], [(0, 1)], 2000, np.random.default_rng(1))
    ci, p, adjusted = stats
    assert count == 2
    np.testing.assert_allclose(delta, [[1 / 3]])
    np.testing.assert_allclose(ci[:, 0, 0], [-1, 1])
    np.testing.assert_allclose(p, adjusted)
    np.testing.assert_allclose(d12.holm([0.04, 0.01, 0.03]), [0.06, 0.03, 0.06])


def test_d12_summary_on_mock_sealed_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(d12, "SPLITS", {"test_seen": 2, "test_unseen": 2})
    monkeypatch.setattr(d12, "DRAWS", 50)
    manifest = {"held_families": {"test_unseen": ["P:unseen"]}, "splits": {}}
    rows = []
    for split in d12.SPLITS:
        key = split.removeprefix("test_")
        tools = [{"name": "f", "parameters": {"properties": {key: {}, "option": {"default": 0}}}}]
        records = []
        for kind in ("call", "refuse"):
            gold = [{"name": "f", "arguments": {key: "x", "option": 0}}] if kind == "call" else []
            records.append({"extra_info": {"id": f"{split}_{kind}", "split": split, "kind": kind,
                                           "n_calls": len(gold), "gold_seen_in_train": bool(gold),
                                           "tools_json": json.dumps(tools)},
                            "reward_model": {"ground_truth": json.dumps(gold)}})
        ids = sorted(r["extra_info"]["id"] for r in records)
        manifest["splits"][split] = {"ids_sha1": hashlib.sha1("\n".join(ids).encode()).hexdigest(),
                                     "n_call": 1, "n_refuse": 1, "n_gold_seen_in_train": 1}
        pq.write_table(pa.Table.from_pylist(records), tmp_path / f"{split}.parquet")
        rows.extend(records)
    test_data = tmp_path / "test.parquet"
    d12.prepare(tmp_path, manifest, test_data)
    models = [{"id": arm, "n8": "1", "prompt": "standard"} for arm in ("e0", "e1")]
    for arm in models:
        for mode, n in (("greedy", 1), ("sample8", 8)):
            path = tmp_path / arm["id"] / mode
            path.mkdir(parents=True)
            config = {"n": n, "temperature": 0 if n == 1 else 1, "top_p": 1, "top_k": -1,
                      "max_tokens": 1024 if n == 1 else 512, "n_prompts": 4, "max_prompt_tokens": None,
                      "enable_thinking": False, "seed": d12.SEED, "native_tools": False, "few_shot": 0}
            (path / "summary.json").write_text(json.dumps(config))
            with (path / "samples.jsonl").open("w") as f:
                for row in reversed(rows):  # Pair by ID even when sample order differs.
                    info = row["extra_info"]
                    gold = row["reward_model"]["ground_truth"]
                    correct = info["kind"] == "refuse"
                    raw = "No suitable tool"
                    if not correct and arm["id"] == "e1":
                        call = json.loads(gold)[0]
                        del call["arguments"]["option"]
                        raw = f"<tool_call>{json.dumps(call)}</tool_call>"
                    outputs = [raw] * n
                    scores = [int(correct)] * n
                    if n == 8 and not correct and arm["id"] == "e1":
                        outputs[-1] = f"<tool_call>{json.dumps(json.loads(gold)[0])}</tool_call>"
                        scores[-1] = 1
                    f.write(json.dumps(dict(id=info["id"], split=info["split"], kind=info["kind"],
                                            gold=gold, outputs=outputs, scores=scores)) + "\n")
    d12.summarize(tmp_path, test_data, manifest, models)
    with (tmp_path / "accuracy.csv").open(newline="") as f:
        report = {(r["mode"], r["model"], r["metric"]): float(r["score"])
                  for r in csv.DictReader(f) if r["stratum"] == "all"}
    assert report["sample8", "e1", "strict_pass_at_1"] == 0.5625
    assert report["sample8", "e1", "strict_pass_at_8"] == 1
    assert report["greedy", "e1", "default_pass_at_1"] == 1
    with (tmp_path / "comparisons.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert any(r["stratum"] == "all" and r["model"] == "e1" and r["reference"] == "e0"
               and r["p_holm"] for r in rows)
