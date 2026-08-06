"""Phase 5: score the tuned configuration on the held-out split, once.

Also runs, for information only and changing nothing: the out-of-sample positive check on
`vie-swv-001` / `vie-swv-003` (preregistration §2.2), the other eight pinned assessment
buildings, and the 19 dropped rows.

Clopper–Pearson intervals are computed here from exact binomial tails rather than imported,
because the project has no scipy dependency and adding one for four numbers would be worse than
twelve lines of bisection. Verified against the published table in preregistration.md §2.1.
"""

from __future__ import annotations

import json
import sys
from math import comb
from pathlib import Path

EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL.parents[1] / "src"))
sys.path.insert(0, str(EVAL))

from score import Detector, confusion, eval_targets, pinned_targets  # noqa: E402

OUT_OF_SAMPLE = ("vie-swv-001", "vie-swv-003")
HELD_OUT_POSITIVE = 298442


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    def solve(f):
        lo, hi = 0.0, 1.0
        for _ in range(300):
            mid = (lo + hi) / 2
            if f(mid) < 0:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    def cdf_le(p):
        return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))

    def cdf_ge(p):
        return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))

    low = 0.0 if k == 0 else solve(lambda p: cdf_ge(p) - alpha / 2)
    high = 1.0 if k == n else solve(lambda p: alpha / 2 - cdf_le(p))
    return low, high


def main() -> None:
    tuned = json.loads((EVAL / "phase4_tuned_config.json").read_text())["tuned"]
    det = Detector()
    report: dict = {"tuned_configuration": tuned}

    # ------------------------------------------------------------- the primary metric, once
    targets = eval_targets("held_out")
    rows = []
    for cache_dir, oid, reference, row in targets:
        result = det.run(cache_dir, oid, tuned)
        rows.append({"objectid": oid, "adresse": row["adresse"], "reference_label": reference,
                     "detected": result["value"] is True, "value": result["value"],
                     "quality": result["quality"]})
    counts = confusion([(r["reference_label"], r["value"]) for r in rows])
    negatives = counts["fp"] + counts["tn"]
    low, high = clopper_pearson(counts["fp"], negatives)
    guard = next(r["detected"] for r in rows if r["objectid"] == HELD_OUT_POSITIVE)
    report["held_out"] = {
        "n": len(rows), "counts": counts,
        "false_positive_rate": {
            "numerator": counts["fp"], "denominator": negatives,
            "point": round(counts["fp"] / negatives, 6),
            "clopper_pearson_95": [round(low, 6), round(high, 6)],
        },
        "degeneracy_guard_298442_detected": guard,
        "threshold_met": counts["fp"] == 0,
        "false_positives": [{"objectid": r["objectid"], "adresse": r["adresse"],
                             "quality": r["quality"]}
                            for r in rows if r["detected"] and r["reference_label"] == "false"],
    }

    # ---------------------------------------------------- reference line: never-fires baseline
    report["degenerate_baseline"] = {
        "description": "a configuration that never fires, scored on the same 35 negatives",
        "false_positives": 0, "denominator": negatives,
        "clopper_pearson_95": [round(v, 6) for v in clopper_pearson(0, negatives)],
        "positives_detected": 0,
    }

    # ------------------------------------------------- out-of-sample positive check (advisory)
    pinned = {}
    for cache_dir, oid, building_id in pinned_targets(det.cfg):
        result = det.run(cache_dir, oid, tuned)
        pinned[building_id] = {"objectid": oid, "detected": result["value"] is True,
                               "value": result["value"], "quality": result["quality"]}
    report["out_of_sample_check"] = {
        building_id: pinned[building_id]["detected"] for building_id in OUT_OF_SAMPLE
    }
    report["pinned_all"] = {k: v["detected"] for k, v in pinned.items()}
    report["pinned_quality"] = {k: v["quality"] for k, v in pinned.items()}

    # ----------------------------------------------------------- dropped rows, information only
    report["dropped"] = {}
    for cache_dir, oid, _reference, row in eval_targets("dropped"):
        result = det.run(cache_dir, oid, tuned)
        report["dropped"][str(oid)] = {"drop_reason": row["drop_reason"],
                                       "adresse": row["adresse"],
                                       "detected": result["value"] is True}

    (EVAL / "phase5_report.json").write_text(json.dumps(report, indent=1) + "\n")

    fp = report["held_out"]["false_positive_rate"]
    print(f"held-out: n={len(rows)}  {json.dumps(counts)}")
    print(f"FPR = {fp['numerator']}/{fp['denominator']} = {fp['point']:.3%}   "
          f"Clopper-Pearson 95% [{low:.3%}, {high:.3%}]")
    print(f"degeneracy guard, 298442 detected: {guard}")
    print(f"threshold met (0 false positives): {report['held_out']['threshold_met']}")
    print("out-of-sample check: " + ", ".join(
        f"{b}={report['out_of_sample_check'][b]}" for b in OUT_OF_SAMPLE))
    print("pinned: " + ", ".join(f"{k[-3:]}={v}" for k, v in report["pinned_all"].items()))
    print("dropped rows fired on: "
          + (", ".join(k for k, v in report["dropped"].items() if v["detected"]) or "none"))


if __name__ == "__main__":
    main()
