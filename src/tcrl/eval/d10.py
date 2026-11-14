"""Summarize D10 and select one checkpoint before D12 is opened."""
import argparse
import json
from math import comb
from pathlib import Path

from tcrl.curriculum.d10 import WEAK
from tcrl.curriculum.freeze import read
from tcrl.eval.d9 import paired


ARMS = ("e0", "cs", "cu", "cf")


def paired_test(treatment, control):
    wins = sum(a > b for a, b in zip(treatment, control, strict=True))
    losses = sum(a < b for a, b in zip(treatment, control, strict=True))
    n = wins + losses
    p = min(1.0, 2 * sum(comb(n, i) for i in range(min(wins, losses) + 1)) / 2**n)
    return {"wins": wins, "losses": losses, "difference": (wins - losses) / len(treatment), "p": p}


def summarize(run, pairs):
    scores, arms = {}, {}
    for arm in ARMS:
        out = run / arm / "eval"
        predictions = read(out / "heldout/samples.jsonl")
        if len(pairs) != 392 or any(len(r["scores"]) != 1 or r["scores"][0] not in (0, 1) for r in predictions):
            raise ValueError("Expected the original 392 held-out pairs with one strict result each")
        report = paired(predictions, pairs)
        by_id = {r["id"]: r["scores"][0] for r in predictions}
        scores[arm] = [by_id[r["id"] + ":conversion"] for r in pairs]
        weak = {}
        for kind in sorted(WEAK):
            entries = [r for r in pairs if kind in r["conversion"].split("+")]
            weak[kind] = {"correct": sum(by_id[r["id"] + ":conversion"] for r in entries), "n": len(entries)}
        dev = json.loads((out / "external_dev541/summary.json").read_bytes())
        bfcl = json.loads((out / "bfcl.score.json").read_bytes())
        if dev["n_prompts"] != 541 or bfcl["all"]["n"] != 1240:
            raise ValueError(f"Incomplete external evaluation for {arm}")
        arms[arm] = {"paired": report, "weak_operations": weak, "dev": dev["acc"],
                     "bfcl": bfcl["all"], "bfcl_categories": bfcl["categories"]}
    comparisons = {}
    for control in ("cs", "cu"):
        result = paired_test(scores["cf"], scores[control])
        result["by_domain"] = {
            domain: sum(a - b for a, b, r in zip(scores["cf"], scores[control], pairs, strict=True)
                        if r["id"].split(":")[1] == domain)
            for domain in sorted({r["id"].split(":")[1] for r in pairs})}
        comparisons[f"cf_vs_{control}"] = result
    adjusted = 0.0
    for index, result in enumerate(sorted(comparisons.values(), key=lambda r: r["p"])):
        adjusted = min(1.0, max(adjusted, result["p"] * (2 - index)))
        result["p_holm"] = adjusted
    # Stable tie-breaks use only already-open D9 and external diagnostics.
    winner = max(("cs", "cu", "cf"), key=lambda arm: (
        sum(scores[arm]), arms[arm]["dev"]["all"], arms[arm]["bfcl"]["correct"]))
    return {"arms": arms, "comparisons": comparisons, "d12_candidate": winner}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--run", type=Path, required=True)
    cli.add_argument("--pairs", type=Path, required=True)
    args = cli.parse_args()
    report = summarize(args.run, read(args.pairs))
    (args.run / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    main()
