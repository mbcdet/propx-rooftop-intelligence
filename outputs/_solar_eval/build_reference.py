"""Phase 3b: turn two readings into one consensus reference standard.

Reads the repaired Phase 3 ``labels.csv``, applies the consensus rule below, writes
``reference_label`` / ``drop_reason`` back into ``labels.csv`` and recomputes ``splits.json``.

The rule, in order. The first line a row meets decides it:

===========================  ==========================  =========================================
assistant_label              human_label                 outcome
===========================  ==========================  =========================================
(any)                        (any)                       ``E1``/``E2`` exclusions win first
committed ``true``/``false`` not reviewed (empty)        drop, ``D2_unreviewed``
``unclear``                  committed                   **use the human label** — one reader
                                                         abstained and the other resolved it.
                                                         An abstention is not a contradiction.
committed, same label        committed, same label       **use it** — consensus
``true`` vs ``false``        (contradicts)               drop, ``D1_disagreement``
===========================  ==========================  =========================================

Nothing here reads or runs the detector, and no row is ever deleted from the file.
"""

import csv
import io
import json
import os
import random

EVAL = os.path.dirname(os.path.abspath(__file__))
LABELS = os.path.join(EVAL, "labels.csv")
SPLITS = os.path.join(EVAL, "splits.json")

SPLIT_SEED = 20260808  # new seed: the scoreable set changed, so the old draw does not apply
TUNE_SHARE = 0.6

CONSENSUS_COMMENT = (
    "# PHASE 3b, consensus reference: reference_label is the final label and is the only column "
    "any score is computed from. It combines the assistant's full-resolution pass-2 reading and "
    "Mohammad's review. Mohammad saw the assistant's proposal, so the readings are anchored rather "
    "than independent and agreement is inflated by an unknown amount; neither reader is "
    "authoritative over the other. drop_reason says why a row carries no reference_label: E1/E2 "
    "are the Phase 3 "
    "exclusions (applied first); D1_disagreement is a direct contradiction, one reader true and "
    "the other false, dropped rather than adjudicated; D2_unreviewed is a row Mohammad never saw. "
    "Where the assistant recorded 'unclear' and Mohammad committed, the human label is used: an "
    "abstention by one reader is not a contradiction. Rows are never deleted."
)


def read_labels():
    with open(LABELS) as fh:
        lines = fh.readlines()
    comments = [line for line in lines if line.startswith("#")]
    body = "".join(line for line in lines if not line.startswith("#"))
    reader = csv.DictReader(io.StringIO(body))
    return comments, list(reader.fieldnames), list(reader)


def resolve(row):
    """Return ``(reference_label, drop_reason, provenance)`` for one row."""
    assistant, human, excluded = row["assistant_label"], row["human_label"], row["excluded_rule"]
    if excluded:
        return "", excluded, "excluded"
    if human == "":
        return "", "D2_unreviewed", "unreviewed"
    if assistant == "unclear":
        return human, "", "human_resolved_assistant_unclear"
    if assistant == human:
        return human, "", "consensus"
    return "", "D1_disagreement", "contradiction"


def main():
    comments, fields, rows = read_labels()

    for field in ("reference_label", "drop_reason"):
        if field not in fields:
            fields.append(field)

    provenance = {}
    for row in rows:
        row["reference_label"], row["drop_reason"], provenance[row["objectid"]] = resolve(row)

    # ------------------------------------------------------------------ inter-reader agreement
    both_committed = [
        r for r in rows
        if r["assistant_label"] in ("true", "false") and r["human_label"] in ("true", "false")
    ]
    both_committed_scoreable = [r for r in both_committed if not r["excluded_rule"]]
    contradictions = [r for r in both_committed if r["assistant_label"] != r["human_label"]]

    # ------------------------------------------------------------------------------- the split
    scoreable = [r for r in rows if r["reference_label"]]
    strata = {
        label: sorted((r["objectid"] for r in scoreable if r["reference_label"] == label), key=int)
        for label in ("true", "false")
    }
    rng = random.Random(SPLIT_SEED)
    tune, held_out = [], []
    for label in ("true", "false"):
        members = list(strata[label])
        rng.shuffle(members)
        cut = round(TUNE_SHARE * len(members))
        tune += members[:cut]
        held_out += members[cut:]
    tune = sorted(tune, key=int)
    held_out = sorted(held_out, key=int)
    assert not set(tune) & set(held_out)
    assert len(tune) + len(held_out) == len(scoreable)

    reference_of = {r["objectid"]: r["reference_label"] for r in rows}

    def count(ids, label):
        return sum(1 for oid in ids if reference_of[oid] == label)

    splits = {
        "seed": SPLIT_SEED,
        "supersedes_seed": 20260807,
        "method": (
            "stratified by reference_label, 60% tune / 40% held-out, "
            "random.Random(seed).shuffle per stratum, cut at round(0.6 * n)"
        ),
        "drawn": "before any detector output was seen; the detector has not been run",
        "scoreable_definition": (
            "reference_label non-empty: a two-reader consensus label surviving the E1/E2 "
            "exclusions, the D1 contradictions and the D2 unreviewed rows"
        ),
        "counts": {
            "rows_total": len(rows),
            "scoreable_total": len(scoreable),
            "positives_total": count(tune + held_out, "true"),
            "negatives_total": count(tune + held_out, "false"),
            "tune_total": len(tune),
            "held_out_total": len(held_out),
            "tune_positives": count(tune, "true"),
            "tune_negatives": count(tune, "false"),
            "held_out_positives": count(held_out, "true"),
            "held_out_negatives": count(held_out, "false"),
        },
        "dropped": {
            reason: sorted((r["objectid"] for r in rows if r["drop_reason"] == reason), key=int)
            for reason in ("E1", "E2", "D1_disagreement", "D2_unreviewed")
        },
        "label_provenance": {
            "consensus": sum(1 for v in provenance.values() if v == "consensus"),
            "human_resolved_assistant_unclear": sum(
                1 for v in provenance.values() if v == "human_resolved_assistant_unclear"
            ),
        },
        "inter_reader": {
            "both_committed": len(both_committed),
            "both_committed_after_exclusions": len(both_committed_scoreable),
            "contradictions": sorted((r["objectid"] for r in contradictions), key=int),
            "disagreement_rate_all": round(len(contradictions) / len(both_committed), 4),
            "disagreement_rate_after_exclusions": round(
                len(contradictions) / len(both_committed_scoreable), 4
            ),
        },
        "tune_objectids": [int(oid) for oid in tune],
        "held_out_objectids": [int(oid) for oid in held_out],
    }

    with open(LABELS, "w", newline="") as fh:
        for line in comments:
            fh.write(line)
        if not any(line.startswith("# PHASE 3b") for line in comments):
            fh.write(CONSENSUS_COMMENT + "\n")
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    with open(SPLITS, "w") as fh:
        json.dump(splits, fh, indent=2)
        fh.write("\n")

    print(json.dumps({k: v for k, v in splits.items() if not k.endswith("objectids")}, indent=2))


if __name__ == "__main__":
    main()
