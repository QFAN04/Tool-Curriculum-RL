import json
from collections import Counter
from pathlib import Path
from tcrl.curriculum.freeze import read, write, gold_key
from tcrl.curriculum.diagnostics import coverage
from tcrl.data.split import gold_families

root = Path("runs/d7_curriculum_frozen")
original = Path("data/processed/gold_disjoint/splits")
sft, rl = (read(original / f"{split}.jsonl") for split in ("sft", "rl"))
seeds = {row["id"]: row for row in rl}
audit = read(Path("runs/d7_curriculum_pilot/filter/audit_schema_review.jsonl"))
reject = {2,14,18,23,26,29,34,35,36,38,41,50,52,53,54,55,65,69,73,77,79,85,92,94,95,98}
uncertain = {4,13,25,27,28,32,37,46,49,57,64,70,74,83,87,88,89,90,96,99}
notes = {
    1: "Preserves PEPE price, transfer networks and recent announcements; the inherited announcements endpoint returns a broad list.",
    4: "Definition and practical advice are present in both questions; the inherited random-advice endpoint cannot guarantee advice about stochastic methods.",
    10: "Preserves prediction from the exact name Anaïs. Generated wording supplies a French-origin cue.",
    18: "Capturing incoming requests becomes subscribing to outage notifications.",
    26: "Reuses the numbers but changes compatible durations into counts of different activities.",
    47: "Preserves the two factors and multiplication intent with swapped contextual roles.",
    53: "Changes tomorrow's weather to current conditions and requests a travel-specific tip.",
    63: "Same movie lookup and title; the user-facing fields requested from the lookup differ.",
    74: "The source requests name-based prediction; the generated question asks to verify actual nationality.",
    85: "The financial statement lookup remains but the generated question adds an unsupported output format.",
    92: "Requests different properties from the original compound lookups.",
    93: "Preserves custom greeting for the ISS crew and a random cat fact.",
}
assert len(audit) == 100 and not reject & uncertain
for index, row in enumerate(audit):
    seed = seeds[row["seed_id"]]
    assert gold_key(row) == gold_key(seed), row["id"]
    row["seed_question"] = seed["question"]
    row["schema_verdict"] = row.pop("semantic_verdict")
    row["schema_note"] = row.pop("review_note")
    verdict = "reject" if index in reject else "uncertain" if index in uncertain else "pass"
    row["semantic_verdict"] = verdict
    row["review_note"] = notes.get(index, {
        "pass": "The generated question preserves the intent of the seed gold calls.",
        "reject": "The generated question drops or changes a substantive request needed for the seed gold calls.",
        "uncertain": "The core subject overlaps, but the full correspondence of the seed gold to the generated request is ambiguous.",
    }[verdict])
write(root / "audit_seed_review.jsonl", audit)
print(Counter((row["tier"], row["semantic_verdict"]) for row in audit))
lookup = {row["id"]: row for row in sft + read(Path("runs/d7_curriculum_combined/candidates.jsonl"))}
frequencies = Counter(family for row in sft + rl for family in gold_families(row))
for arm in ("cs", "cu", "cf"):
    rows = [lookup[row["id"]] for row in read(root / f"{arm}_sft.jsonl")]
    result = coverage(rows, frequencies)
    print(arm, result["low_frequency_count"], result["combination_entropy_bits"])
