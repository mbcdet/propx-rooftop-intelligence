"""Build the Phase 2 labels.csv and the human review queue.

Reads the per-batch assistant judgements written during the full-resolution pass,
applies the recalibrated confidences, computes the review queue, draws the blind
control sample with a fixed seed and writes the CSV. No pipeline code is touched.
"""

import csv
import glob
import json
import os
import random

EVAL = os.path.dirname(os.path.abspath(__file__))
JUDGEMENTS = os.path.join(EVAL, "pass2_judgements")
SEED = 20260806

ZONE_OF_PREFIX = {
    "spengergasse": "Spengergasse, 1050",
    "tu_karlsplatz": "TU Wien / Karlsplatz, 1040",
    "wu_wien": "WU Wien campus, Welthandelsplatz 1, 1020",
}

# ---------------------------------------------------------------- pass 2 input
judgements = {}
for path in sorted(glob.glob(os.path.join(JUDGEMENTS, "batch*.json"))):
    with open(path) as fh:
        for row in json.load(fh):
            judgements[row["crop"]] = row

with open(os.path.join(EVAL, "calibration.json")) as fh:
    cal = json.load(fh)
for crop, conf in cal["conf"].items():
    judgements[crop]["confidence"] = conf
mimics = set(cal["mimic"])

# ---------------------------------------------------------------- pass 1 input
with open(os.path.join(EVAL, "labels_pass1.csv")) as fh:
    header_comment = fh.readline().rstrip("\n")
    pass1 = {int(r["objectid"]): r for r in csv.DictReader(fh)}

crops = sorted(os.path.basename(p)[:-4] for p in glob.glob(os.path.join(EVAL, "crops", "*.png")))
assert len(crops) == 109, len(crops)
assert set(crops) == set(judgements), set(crops) ^ set(judgements)

rows = []
for crop in crops:
    prefix, oid = crop.rsplit("_", 1)
    oid = int(oid)
    p1 = pass1[oid]
    j = judgements[crop]
    rows.append({
        "crop": crop,
        "zone": ZONE_OF_PREFIX[prefix],
        "objectid": oid,
        "adresse": p1["adresse"],
        "dachform": p1["dachform"],
        "slope_mean_deg": p1["slope_mean_deg"],
        "coverage_pct": p1["coverage_pct"],
        "assistant_label": j["label"],
        "assistant_confidence": j["confidence"],
        "assistant_reason": j["reason"],
        "pass1_label": p1["proposed_label"],
        "mimic": crop in mimics,
    })

# ------------------------------------------------------------------ the queue
# Criteria, applied in order; each row is attributed to the first one it meets.
g1 = [r for r in rows if r["assistant_label"] == "true"]
g2 = [r for r in rows if r["assistant_label"] == "unclear"]
g3 = [r for r in rows if r not in g1 + g2 and r["assistant_confidence"] in ("low", "medium")]
g4 = [r for r in rows if r not in g1 + g2 + g3 and r["mimic"]]
rest = [r for r in rows if r not in g1 + g2 + g3 + g4]
assert all(r["assistant_label"] == "false" and r["assistant_confidence"] == "high" for r in rest)

rng = random.Random(SEED)
control = rng.sample(sorted(rest, key=lambda r: r["objectid"]), 15)
control_ids = {r["objectid"] for r in control}
never_reviewed = [r for r in rest if r["objectid"] not in control_ids]

# Review criteria 1-4 in their declared order, then the control sample shuffled in among them so
# its members are not identifiable by position.
queue = g1 + g2 + g3 + g4
for r in control:
    queue.insert(rng.randint(0, len(queue)), r)

ordered = queue + sorted(never_reviewed, key=lambda r: (r["zone"], r["objectid"]))
assert len(ordered) == 109

# ----------------------------------------------------------------- write CSV
cols = [
    "review_order",
    "zone",
    "objectid",
    "adresse",
    "dachform",
    "slope_mean_deg",
    "coverage_pct",
    "assistant_label",
    "assistant_confidence",
    "assistant_reason",
    "in_review_queue",
    "human_label",
    "human_note",
]

seed_comment = (
    "# PASS 2, full-resolution: assistant_label is the assistant's own reading of each crop "
    "opened individually at native resolution (and zoomed 2-6x where a mimic had to be "
    "adjudicated), NOT the output of the detector under test, which has not been run and must "
    "not see this file before scoring. human_label is Mohammad's and remains the only column "
    "that carries a label. in_review_queue=yes marks the rows he is asked to check; the queue "
    "holds every 'true', every 'unclear', every low/medium confidence row, every row whose "
    "reason names a mimic that had to be adjudicated on that roof, plus a BLIND CONTROL SAMPLE "
    "of 15 high-confidence 'false' rows drawn with random.Random(seed).sample, seed=20260806, "
    "and shuffled in among the rest. The control estimates the assistant's error rate on the "
    "20 rows nobody reviews. Rows are sorted by review_order."
)

never_reviewed_ids = {row["objectid"] for row in never_reviewed}

with open(os.path.join(EVAL, "labels.csv"), "w", newline="") as fh:
    fh.write(header_comment + "\n")
    fh.write(seed_comment + "\n")
    w = csv.DictWriter(fh, fieldnames=cols)
    w.writeheader()
    for i, r in enumerate(ordered, start=1):
        r["review_order"] = i
        w.writerow({
            "review_order": i,
            "zone": r["zone"],
            "objectid": r["objectid"],
            "adresse": r["adresse"],
            "dachform": r["dachform"],
            "slope_mean_deg": r["slope_mean_deg"],
            "coverage_pct": r["coverage_pct"],
            "assistant_label": r["assistant_label"],
            "assistant_confidence": r["assistant_confidence"],
            "assistant_reason": r["assistant_reason"],
            "in_review_queue": "yes" if r["objectid"] not in never_reviewed_ids else "no",
            "human_label": "",
            "human_note": "",
        })

# ------------------------------------------------------------------- summary
summary = {
    "seed": SEED,
    "counts": {
        k: sum(1 for r in rows if r["assistant_label"] == k)
        for k in ("true", "false", "unclear")
    },
    "per_zone": {
        zone: {
            label: sum(
                1
                for row in rows
                if row["zone"] == zone and row["assistant_label"] == label
            )
            for label in ("true", "false", "unclear")
        }
        for zone in sorted(ZONE_OF_PREFIX.values())
    },
    "confidence": {
        k: sum(1 for r in rows if r["assistant_confidence"] == k)
        for k in ("high", "medium", "low")
    },
    "queue": {
        "criterion_1_true": len(g1),
        "criterion_2_unclear": len(g2),
        "criterion_3_low_medium": len(g3),
        "criterion_4_mimics": len(g4),
        "criterion_5_control": len(control),
        "total": len(queue),
        "never_reviewed": len(never_reviewed),
    },
    "control_objectids": sorted(control_ids),
    "changed_pass1_to_pass2": [
        {
            "objectid": r["objectid"],
            "adresse": r["adresse"],
            "from": r["pass1_label"],
            "to": r["assistant_label"],
        }
        for r in rows
        if r["pass1_label"] != r["assistant_label"]
    ],
    "queue_order_ids": [r["objectid"] for r in queue],
}
with open(os.path.join(EVAL, "summary.json"), "w") as fh:
    json.dump(summary, fh, indent=2)

compact_summary = {
    key: value
    for key, value in summary.items()
    if key not in ("changed_pass1_to_pass2", "queue_order_ids", "control_objectids")
}
print(json.dumps(compact_summary, indent=2))
print("changed:", len(summary["changed_pass1_to_pass2"]))
