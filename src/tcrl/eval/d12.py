"""Prepare the frozen test input on the GPU host and summarize D12 outputs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from tcrl.curriculum.seeds import strip_optional_defaults
from tcrl.data.split import tool_family
from tcrl.reward import calls_match
from tcrl.tool_env import parse_tool_calls, tool_index


SPLITS = {"test_seen": 1176, "test_unseen": 1842}
SEED = 20261113
DRAWS = 20000
ADDITIONAL_COMPARISONS = (
    ("e1", "e0plus"), ("e2a", "e1"), ("e2b", "e2a"),
    ("e2b_gold_only", "e2b"), ("x2", "e1"),
    ("d7_cf", "d7_cu"), ("d7_cf", "d7_cs"), ("d8_cf", "d8_cu"),
    ("d8_cf", "d8_cs"), ("d8_cf", "d8_csplus"), ("d9_binary", "d9_sft"),
    ("d9_graded", "d9_sft"), ("d9_graded", "d9_binary"),
)


def read_models(path):
    with path.open(encoding="utf-8", newline="") as f:
        entries = list(csv.DictReader(f, delimiter="\t"))
    ids = [row["id"] for row in entries]
    if (len(ids) != 19 or len(set(ids)) != 19 or "e0" not in ids
            or Counter(row["n8"] for row in entries) != {"1": 14, "0": 5}
            or any(row["prompt"] not in {"standard", "native", "fewshot3"} for row in entries)
            or {arm for pair in ADDITIONAL_COMPARISONS for arm in pair} - set(ids)):
        raise ValueError("Expected 19 distinct frozen models, 14 with n=8")
    return entries


def prepare(source, manifest, output):
    rows = []
    for split, count in SPLITS.items():
        table = pq.read_table(source / f"{split}.parquet")
        info = table["extra_info"].to_pylist()
        ids = [row["id"] for row in info]
        digest = hashlib.sha1("\n".join(sorted(ids)).encode()).hexdigest()
        shape = manifest["splits"][split]
        if (len(ids) != count or len(set(ids)) != count
                or digest != shape["ids_sha1"] or any(row["split"] != split for row in info)
                or Counter(row["kind"] for row in info) != {"call": shape["n_call"], "refuse": shape["n_refuse"]}
                or (split == "test_seen" and sum(row["kind"] == "call" and row["gold_seen_in_train"]
                    for row in info) != shape["n_gold_seen_in_train"])):
            raise ValueError(f"Unexpected sealed inventory for {split}")
        rows.append(table)
    combined = pa.concat_tables(rows)
    ids = [row["id"] for row in combined["extra_info"].to_pylist()]
    if len(set(ids)) != len(ids):
        raise ValueError("Test splits have overlapping IDs")
    pq.write_table(combined, output)


def cluster(row, tools, gold, held):
    info = row["extra_info"]
    schemas = tool_index(tools)
    families = {tool_family(schemas[call["name"]]) for call in gold}
    if not families:
        families = {tool_family(tool) for tool in schemas.values()}
        if info["split"] == "test_unseen":
            families &= held
    # Match the split builder's lexicographically first gold-family convention.
    return min(families, default="no_visible_family")


def strata(row):
    info = row["extra_info"]
    split, kind = info["split"], info["kind"]
    calls = f"calls={min(info['n_calls'], 3)}" + ("+" if info["n_calls"] >= 3 else "")
    yield from ("all", split, f"kind={kind}", f"{split}|kind={kind}", calls, f"{split}|{calls}")
    if split == "test_seen" and kind == "call":
        yield f"{split}|gold_seen_in_train={str(info['gold_seen_in_train']).lower()}"


def score(outputs, gold, tools):
    expected = strip_optional_defaults(gold, tools)
    strict, equivalent = [], []
    for output in outputs:
        parsed = parse_tool_calls(output)
        exact = parsed.ok and calls_match(parsed.calls, gold)
        strict.append(int(exact))
        equivalent.append(int(exact or (parsed.ok and calls_match(
            strip_optional_defaults(parsed.calls, tools), expected))))
    return strict, equivalent


def read_scores(path, rows, n, out):
    positions = {row["extra_info"]["id"]: i for i, row in enumerate(rows)}
    scores = np.empty((len(rows), 2, n), dtype=np.uint8)
    seen = set()
    with path.open(encoding="utf-8") as source, out.open("w", encoding="utf-8") as target:
        for line in source:
            sample = json.loads(line)
            sample_id = sample["id"]
            if sample_id in seen or sample_id not in positions or len(sample["outputs"]) != n:
                raise ValueError(f"Unexpected, repeated or incomplete sample {sample_id} in {path}")
            i = positions[sample_id]
            row = rows[i]
            info = row["extra_info"]
            truth = row["reward_model"]["ground_truth"]
            if (sample["split"], sample["kind"], sample["gold"]) != (info["split"], info["kind"], truth):
                raise ValueError(f"Sample metadata mismatch for {sample_id} in {path}")
            strict, equivalent = score(sample["outputs"], json.loads(truth), json.loads(info["tools_json"]))
            if strict != sample["scores"]:
                raise ValueError(f"Strict re-score mismatch for {sample_id} in {path}")
            scores[i] = strict, equivalent
            target.write(json.dumps({"id": sample_id, "strict": strict, "default_equivalence": equivalent}) + "\n")
            seen.add(sample_id)
    if len(seen) != len(rows):
        raise ValueError(f"Incomplete test coverage in {path}")
    return scores


def holm(p_values):
    order = np.argsort(p_values)
    adjusted = np.empty(len(order))
    adjusted[order] = np.minimum(1, np.maximum.accumulate(
        np.asarray(p_values)[order] * np.arange(len(order), 0, -1)))
    return adjusted


def cluster_statistics(values, clusters, comparisons, draws, rng):
    """Paired percentile bootstrap and centered two-sided bootstrap tests."""
    levels, inv = np.unique(clusters, return_inverse=True)
    weights = np.bincount(inv)
    totals = np.zeros((len(levels), *values.shape[1:]))
    np.add.at(totals, inv, values)
    means = values.mean(axis=0)
    diffs = np.stack([means[a] - means[b] for a, b in comparisons])
    if len(levels) < 2:
        return means, diffs, None, len(levels)
    counts = rng.multinomial(len(levels), np.full(len(levels), 1 / len(levels)), size=draws)
    estimates = (counts @ totals.reshape(len(levels), -1)) / (counts @ weights)[:, None]
    estimates = estimates.reshape(draws, *means.shape)
    paired = np.stack([estimates[:, a] - estimates[:, b] for a, b in comparisons], axis=1)
    ci = np.quantile(paired, [0.025, 0.975], axis=0)
    exceed = (np.abs(paired - diffs) + 1e-12 >= np.abs(diffs)).sum(axis=0)
    p = (exceed + 1) / (draws + 1)
    adj = holm(p.ravel()).reshape(p.shape)
    return means, diffs, (ci, p, adj), len(levels)


def summarize(root, data, manifest, models):
    rows = pq.read_table(data).to_pylist()
    held = set(manifest["held_families"]["test_unseen"])
    clusters = np.array([cluster(row, json.loads(row["extra_info"]["tools_json"]),
                                 json.loads(row["reward_model"]["ground_truth"]), held) for row in rows])
    groups = defaultdict(list)
    for i, row in enumerate(rows):
        for label in strata(row):
            groups[label].append(i)
    rng = np.random.default_rng(SEED)
    accuracy, comparisons = [], []
    for mode, n in (("greedy", 1), ("sample8", 8)):
        entries = [row for row in models if n == 1 or row["n8"] == "1"]
        ids = [row["id"] for row in entries]
        arrays = []
        for entry in entries:
            arm = entry["id"]
            path = root / arm / mode
            config = json.loads((path / "summary.json").read_bytes())
            prompt = entry["prompt"]
            expected = {"n": n, "temperature": 0.0 if n == 1 else 1.0, "top_p": 1.0, "top_k": -1,
                        "max_tokens": 1024 if n == 1 else 512, "n_prompts": len(rows),
                        "max_prompt_tokens": None, "enable_thinking": False, "seed": SEED,
                        "native_tools": prompt == "native", "few_shot": 3 if prompt == "fewshot3" else 0}
            if any(config.get(key) != value for key, value in expected.items()):
                raise ValueError(f"Frozen settings mismatch for {arm}/{mode}")
            output = read_scores(path / "samples.jsonl", rows, n, path / "d12_scores.jsonl")
            arrays.append(output.mean(axis=2))
            if n == 8:
                arrays[-1] = np.column_stack([arrays[-1], output.max(axis=2)])
        values = np.stack(arrays, axis=1)
        metric_names = ["strict_pass_at_1", "default_pass_at_1"]
        if n == 8:
            metric_names += ["strict_pass_at_8", "default_pass_at_8"]
        pairs = [(arm, "e0") for arm in ids if arm != "e0"]
        pairs += [(a, b) for a, b in ADDITIONAL_COMPARISONS if a in ids and b in ids]
        index = [(ids.index(a), ids.index(b)) for a, b in pairs]
        for label, subset in sorted(groups.items()):
            means, diffs, stats, n_clusters = cluster_statistics(
                values[subset], clusters[subset], index, DRAWS, rng)
            detail = dict(mode=mode, stratum=label, n=len(subset), clusters=n_clusters)
            for i, arm in enumerate(ids):
                for j, metric in enumerate(metric_names):
                    denom = len(subset) * (8 if n == 8 and j < 2 else 1)
                    accuracy.append(dict(**detail, model=arm, metric=metric,
                                         score=float(means[i, j]), correct=round(means[i, j] * denom),
                                         denominator=denom))
            for i, (arm, reference) in enumerate(pairs):
                for j, metric in enumerate(metric_names):
                    extra = dict(ci_low=None, ci_high=None, p=None, p_holm=None)
                    if stats is not None:
                        ci, p, adj = stats
                        extra.update(ci_low=float(ci[0, i, j]), ci_high=float(ci[1, i, j]),
                                     p=float(p[i, j]), p_holm=float(adj[i, j]))
                    comparisons.append(dict(**detail, model=arm, reference=reference, metric=metric,
                                            delta=float(diffs[i, j]), **extra))
    for name, records in (("accuracy", accuracy), ("comparisons", comparisons)):
        with (root / f"{name}.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check-models", "prepare", "summarize"))
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    models = read_models(args.models)
    if args.mode == "check-models":
        return
    manifest = json.loads(args.manifest.read_bytes())
    if args.mode == "prepare":
        prepare(args.source, manifest, args.data)
    else:
        summarize(args.root, args.data, manifest, models)


if __name__ == "__main__":
    main()
