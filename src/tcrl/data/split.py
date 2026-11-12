"""按工具族划分数据，保证评测里的「未见工具族」真的没在训练里出现过。

工具族怎么定：
  Toucan 里同一个工具常常挂在不同名字的 MCP server 下，函数名也会略有出入
  （`get_live_temp` / `getLiveTemperature`，参数都是 latitude、longitude）。
  所以族的定义取保守的一种：**参数名集合相同的工具算同一族**；没有参数的工具按函数名算。
  这样分得偏粗（比如所有只有一个 `query` 参数的搜索工具是一族），但方向是安全的：
  同一族要么全在训练侧、要么全被留出，不会出现「换了个名字的同一个工具」跨到两边。

  试过并放弃的做法：按样本里工具的共现做连通分量。因为大量样本同时带多个 server 的工具，
  结果九成样本并成了一个族，没法留出。

留出规则：
  随机选一批族作为留出族 H。
  - 标准调用用到的工具**全部**属于 H 的样本 -> test_unseen / dev_unseen；
  - 标准调用和**可见工具列表**里都不含 H 中任何族的样本 -> 训练侧；
  - 其余（部分沾到 H）丢弃。
  所以训练侧既没调用过、也没见过留出族工具的定义。

划分：
  test_unseen / dev_unseen : 留出族
  test_seen   / dev_seen   : 训练侧里留出的样本
  sft                      : SFT 用
  rl                       : RL 题池（也是课程生成的种子）
训练侧每个族最多取 cap_per_family 条，避免个别工具（天气、词典各两千多条）主导数据。
拒绝样本（kind="refuse"）只要可见工具里不含留出族就归训练侧，否则归留出侧，并按比例下采样。

用法：python -m tcrl.data.split --inp data/processed/all.jsonl --out data/processed/splits --manifest data/manifests/splits.json
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random

from ..tool_env import canonical

SEED = 20261113


def tool_family(tool: dict) -> str:
    props = (tool.get("parameters") or {}).get("properties") or {}
    if props:
        return "P:" + ",".join(sorted(props))
    return "N:" + tool["name"].rsplit("-", 1)[-1].lower()


def gold_families(r: dict) -> set[str]:
    idx = {t["name"]: t for t in r["tools"]}
    return {tool_family(idx[c["name"]]) for c in r["gold"]}


def visible_families(r: dict) -> set[str]:
    return {tool_family(t) for t in r["tools"]}


def gold_key(r: dict) -> str:
    return "|".join(sorted(canonical(c) for c in r["gold"]))


def make_splits(
    records: list[dict],
    unseen_test_frac: float = 0.15,
    unseen_dev_frac: float = 0.05,
    n_test_seen: int = 1000,
    n_dev_seen: int = 500,
    sft_frac: float = 0.5,
    refuse_ratio: float = 0.15,
    max_family_frac: float = 0.03,
    cap_per_family: int = 300,
) -> dict:
    rng = random.Random(SEED)
    calls = [r for r in records if r["kind"] == "call"]
    for r in calls:
        r["gold_families"] = sorted(gold_families(r))
    fam_count = collections.Counter(f for r in calls for f in r["gold_families"])
    total = len(calls)

    # 选留出族：太大的族留在训练侧，否则一个族就能吃掉整个留出配额
    fam_ids = sorted(fam_count)
    rng.shuffle(fam_ids)
    held = {"test_unseen": set(), "dev_unseen": set()}
    quota = {"test_unseen": unseen_test_frac * total, "dev_unseen": unseen_dev_frac * total}
    got: collections.Counter = collections.Counter()
    for f in fam_ids:
        if fam_count[f] > max_family_frac * total:
            continue
        for name in ("test_unseen", "dev_unseen"):
            if got[name] < quota[name]:
                held[name].add(f)
                got[name] += fam_count[f]
                break
    held_all = held["test_unseen"] | held["dev_unseen"]

    out: dict = collections.defaultdict(list)
    train_side, dropped = [], 0
    for r in calls:
        g = set(r["gold_families"])
        if g <= held["test_unseen"]:
            out["test_unseen"].append(r)
        elif g <= held["dev_unseen"]:
            out["dev_unseen"].append(r)
        elif not (visible_families(r) & held_all):
            train_side.append(r)
        else:
            dropped += 1
    # test 与 dev 的留出族互不可见
    out["test_unseen"] = [r for r in out["test_unseen"] if not (visible_families(r) & held["dev_unseen"])]
    out["dev_unseen"] = [r for r in out["dev_unseen"] if not (visible_families(r) & held["test_unseen"])]

    # 训练侧按族封顶（按样本的第一个标准调用所在族计数）
    rng.shuffle(train_side)
    seen_n: collections.Counter = collections.Counter()
    capped = []
    for r in train_side:
        key = r["gold_families"][0]
        if seen_n[key] < cap_per_family:
            seen_n[key] += 1
            capped.append(r)
    out["test_seen"] = capped[:n_test_seen]
    out["dev_seen"] = capped[n_test_seen : n_test_seen + n_dev_seen]
    rest = capped[n_test_seen + n_dev_seen :]
    k = int(sft_frac * len(rest))
    out["sft"], out["rl"] = rest[:k], rest[k:]

    # 训练侧留出的样本打标：标准调用是否在训练数据里原样出现过（区分「改写」与「新参数组合」）
    train_gold = {gold_key(r) for r in out["sft"] + out["rl"]}
    for name in ("test_seen", "dev_seen"):
        for r in out[name]:
            r["gold_seen_in_train"] = gold_key(r) in train_gold

    # 拒绝样本
    pools: dict = collections.defaultdict(list)
    for r in records:
        if r["kind"] != "refuse":
            continue
        v = visible_families(r)
        if not (v & held_all):
            pools["train"].append(r)
        elif (v & held["test_unseen"]) and not (v & held["dev_unseen"]):
            pools["test_unseen"].append(r)
        elif (v & held["dev_unseen"]) and not (v & held["test_unseen"]):
            pools["dev_unseen"].append(r)
    for p in pools.values():
        rng.shuffle(p)
    pos = 0
    for name in ("test_unseen", "dev_unseen", "test_seen", "dev_seen", "sft", "rl"):
        want = int(round(refuse_ratio / (1 - refuse_ratio) * len(out[name])))
        if name in ("test_unseen", "dev_unseen"):
            out[name] += pools[name][:want]
        else:
            out[name] += pools["train"][pos : pos + want]
            pos += want
        rng.shuffle(out[name])
    out["_meta"] = {
        "held_families": {k: sorted(v) for k, v in held.items()},
        "n_call_total": total,
        "n_dropped_partial_overlap": dropped,
        "n_train_side_before_cap": len(train_side),
        "n_families_total": len(fam_count),
    }
    return out


def check_isolation(splits: dict) -> None:
    """训练侧任何样本的可见工具都不含留出族；test 与 dev 的留出族互不可见；样本不跨 split。"""
    held = {k: set(v) for k, v in splits["_meta"]["held_families"].items()}
    held_all = held["test_unseen"] | held["dev_unseen"]
    assert not (held["test_unseen"] & held["dev_unseen"])
    for name in ("sft", "rl", "test_seen", "dev_seen"):
        for r in splits[name]:
            assert not (visible_families(r) & held_all), (name, r["id"])
    for r in splits["test_unseen"]:
        assert not (visible_families(r) & held["dev_unseen"]), r["id"]
        if r["kind"] == "call":
            assert gold_families(r) <= held["test_unseen"], r["id"]
    for r in splits["dev_unseen"]:
        assert not (visible_families(r) & held["test_unseen"]), r["id"]
        if r["kind"] == "call":
            assert gold_families(r) <= held["dev_unseen"], r["id"]
    ids = [r["id"] for k, v in splits.items() if k != "_meta" for r in v]
    assert len(ids) == len(set(ids)), "an example appears in two splits"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inp", default="data/processed/all.jsonl")
    ap.add_argument("--out", default="data/processed/splits")
    ap.add_argument("--manifest", default="data/manifests/splits.json")
    a = ap.parse_args()
    records = [json.loads(line) for line in open(a.inp, encoding="utf-8")]
    splits = make_splits(records)
    check_isolation(splits)
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(os.path.dirname(a.manifest), exist_ok=True)
    manifest = {"seed": SEED, **splits["_meta"], "splits": {}}
    for name, rows in splits.items():
        if name == "_meta":
            continue
        with open(f"{a.out}/{name}.jsonl", "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        kinds = collections.Counter(r["kind"] for r in rows)
        info = {
            "n": len(rows),
            "n_call": kinds["call"],
            "n_refuse": kinds["refuse"],
            "n_gold_families": len({f for r in rows if r["kind"] == "call" for f in r["gold_families"]}),
            "n_multi_call": sum(len(r["gold"]) > 1 for r in rows),
            "ids_sha1": hashlib.sha1("\n".join(sorted(r["id"] for r in rows)).encode()).hexdigest(),
        }
        if name.endswith("_seen"):
            info["n_gold_seen_in_train"] = sum(bool(r.get("gold_seen_in_train")) for r in rows)
        manifest["splits"][name] = info
    json.dump(manifest, open(a.manifest, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in manifest.items() if k != "held_families"}, indent=1))


if __name__ == "__main__":
    main()
