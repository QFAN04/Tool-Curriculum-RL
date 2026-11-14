"""Paired D9 evaluation and preregistered gate diagnostics."""
import argparse
import json
from pathlib import Path

from tcrl.curriculum.freeze import read


def paired(rows, pairs):
    scores = {r["id"]: sum(r["scores"]) / len(r["scores"]) for r in rows}
    expected = {f"{r['id']}:{v}" for r in pairs for v in ("literal", "conversion")}
    if not pairs or len(scores) != len(rows) or scores.keys() != expected:
        raise ValueError("Predictions do not exactly cover unique pair variants")
    def metrics(records):
        values = [(scores[r["id"] + ":literal"], scores[r["id"] + ":conversion"]) for r in records]
        n = len(values)
        literal, conversion = (sum(v[i] for v in values) / n for i in range(2))
        return {"n": n, "literal": literal, "conversion": conversion, "gap": literal - conversion,
                "literal_only": sum(a == 1 and b == 0 for a, b in values),
                "conversion_only": sum(b == 1 and a == 0 for a, b in values)}
    kinds = sorted({r["conversion"] for r in pairs})
    return {"all": metrics(pairs),
            "by_conversion": {kind: metrics([r for r in pairs if r["conversion"] == kind]) for kind in kinds},
            "by_domain": {domain: metrics([r for r in pairs if r["id"].split(":")[1] == domain])
                          for domain in sorted({r["id"].split(":")[1] for r in pairs if ":" in r["id"]})}}


def call_mixed_fraction(samples):
    groups = [r["scores"] for r in samples if r["kind"] == "call"]
    if not groups or any(len(g) != 8 or any(s not in (0, 1) for s in g) for g in groups):
        raise ValueError("Expected eight strict scores for every training call")
    return sum(0 < sum(g) < 8 for g in groups) / len(groups)


def thinking_pass(summary):
    return (summary["n_prompts"] == 20 and summary["n"] == 1
            and summary["thinking"]["nonempty_closed_fraction"] >= .80
            and summary["thinking"]["closed_thinking_fraction"] >= .90
            and summary["truncated_fraction"] <= .10)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cmd = cli.add_subparsers(required=True, dest="command")
    mode = cmd.add_parser("thinking")
    mode.add_argument("summaries", nargs="+", type=Path)
    mode = cmd.add_parser("paired")
    mode.add_argument("--samples", type=Path, required=True)
    mode.add_argument("--pairs", type=Path, required=True)
    mode.add_argument("--probe", type=Path)
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()
    if args.command == "thinking":
        checks = {str(path): thinking_pass(json.loads(path.read_text())) for path in args.summaries}
        result = {"by_model": checks, "pass": all(checks.values())}
    else:
        result = paired(read(args.samples), read(args.pairs))
        if args.probe:
            mixed = call_mixed_fraction(read(args.probe))
            result["call_mixed_fraction"] = mixed
            core = result["all"]
            result["pass"] = core["conversion"] <= .65 and core["gap"] >= .15 and mixed >= .20
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    main()
