import json
from pathlib import Path

import pytest

from tcrl.curriculum import d10
from tcrl.eval.d10 import paired_test


def test_seed_pool_reuses_strict_d9_trajectories():
    data = Path("runs/d9_stage0")
    archive = Path("runs/d9-direct-gate-43482.tar.gz")
    if not (data / "manifest.json").is_file() or not archive.is_file():
        pytest.skip("Local D9 artifacts are unavailable")
    _, scores = d10.checked_inputs(data, archive)
    pool, static, replay = d10.seed_pool(d10.read(data / "train_records.jsonl"), scores)
    assert len(pool) == 700
    assert sum(r["pass_count"] == 8 for r in pool) == 138
    assert sum(r["pass_count"] is None for r in pool) == 366
    assert len(static) == 360 and len(replay) == 64
    assert all(r["mode"] == "literal" or r["pass_count"] == 8 for r in static)
    assert all(r["kind"] == "refuse" for r in replay)
    assert len(d10.draw_seeds(pool, "cu")) == len(d10.draw_seeds(pool, "cf")) == 1200
    assert all(0 <= r["pass_count"] < 8 for r in d10.draw_seeds(pool, "cf"))


def test_structured_generation_solves_discount_and_negative_offset():
    seed = {"id": "seed", "domain": "gallery", "conversion": "rate_total+relative_time",
            "mode": "conversion", "pass_count": 0}
    spec = d10.suggested_schema(seed, 9)["properties"]
    fields = {key: info["maximum"] for key, info in spec.items()}
    fields.update(x2=2, y1=0)
    row = d10.materialize(seed, json.dumps(fields), 9, "cf")
    assert len(row["tools"]) == 8 and len(row["gold"]) == 3
    assert row["gold"][1]["arguments"] == {"gallery_total_cents": fields["x0"] * fields["x1"] * 90}
    assert row["gold"][2]["arguments"] == {"gallery_offset_seconds": -fields["y0"] * 3600}
    assert "10% discount" in row["question"] and "hours before" in row["question"]
    assert all(t["name"].startswith("gallery_") for t in row["tools"])
    literal = d10.materialize({**seed, "mode": "literal"}, json.dumps(fields), 9, "cu")
    assert literal["gold"] == row["gold"] and "10% discount" not in literal["question"]
    with pytest.raises(ValueError, match="operand schema"):
        d10.materialize(seed, json.dumps({**fields, "x0": True}), 9, "cf")
    with pytest.raises(KeyError):
        d10.materialize({**seed, "domain": "stadium"}, json.dumps(fields), 9, "cf")


def test_freeze_keeps_mixed_and_pads_from_all_right(tmp_path, monkeypatch):
    monkeypatch.setattr(d10, "TRAIN_CALLS", 2)
    monkeypatch.setattr(d10, "REPLAY", 1)
    seed = {"id": "seed", "domain": "gallery", "conversion": "duration+rate_total",
            "mode": "conversion", "pass_count": 0}
    candidates = {}
    probe_rows = []
    for arm in d10.ARMS:
        candidates[arm] = []
        for i, hits in enumerate((0, 2, 8, 8)):
            schema = d10.suggested_schema(seed, i)["properties"]
            program = {key: spec["maximum"] for key, spec in schema.items()}
            row = d10.materialize(seed, json.dumps(program), i, arm)
            candidates[arm].append(row)
            probe_rows.append({"id": row["id"], "scores": [1] * hits + [0] * (8 - hits)})
        d10.write(tmp_path / f"{arm}_candidates.jsonl", candidates[arm])
    d10.write(tmp_path / "static.jsonl", candidates["cu"][:2])
    d10.write(tmp_path / "replay.jsonl", [
        {"id": "replay", "kind": "refuse", "tools": [], "question": "Open the window.", "refusal": "No tool."},
    ])
    probe = tmp_path / "probe.jsonl"
    d10.write(probe, probe_rows)
    d10.freeze(tmp_path, probe)
    for arm in ("cs", "cu", "cf"):
        assert len(d10.read(tmp_path / f"{arm}_sft.jsonl")) == 3
    selected = {r["id"] for r in d10.read(tmp_path / "cf_selected.jsonl")}
    assert "d10:cf:0001" in selected and "d10:cf:0000" not in selected
    d10.write(probe, probe_rows[:-1])
    with pytest.raises(ValueError, match="candidate IDs"):
        d10.freeze(tmp_path, probe)


def test_paired_exact_uses_only_discordant_pairs():
    assert paired_test([1, 1, 1, 0, 0], [0, 0, 1, 0, 0]) == {
        "wins": 2, "losses": 0, "difference": 0.4, "p": 0.5,
    }
