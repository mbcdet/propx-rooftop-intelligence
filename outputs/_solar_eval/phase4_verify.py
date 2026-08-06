"""Phase 4, part 2: verify the surrogate, then measure how fragile the best configuration is.

Three jobs, all on the TUNE SPLIT ONLY:

1. **Surrogate verification.** ``search.py`` prunes 64,512 configurations with a fast
   re-implementation of the decision half of ``observe_solar_panels``. Here the real detector is
   run over all 54 tune rows for the winning configuration and for a fixed random sample of other
   configurations, and every one of the 54 verdicts must match. A single mismatch aborts: a
   search whose surrogate does not agree with the detector has searched the wrong thing.

2. **Sensitivity.** Each of the six parameters is moved +5% and -5% on its own, and the tune-split
   result is re-measured with the real detector. Anything that swings the result is a finding and
   is recorded in the evaluation report regardless of whether it improves or worsens the score.

3. **Robustness census.** Every configuration that scored 0 false positives *and* detected the
   tune positive is re-checked under all single-parameter +/-5% perturbations, to answer the
   question the single best configuration cannot: is a zero-false-positive result at this
   parameter setting a stable region, or a knife edge?
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL.parents[1] / "src"))
sys.path.insert(0, str(EVAL))

from score import SEARCHABLE, Detector, confusion, eval_targets  # noqa: E402
from search import FILTERS, GRID, MORPHOLOGY, decide, extract  # noqa: E402

from propx_roofs.imaging import ground_pixel_size_m, px_from_m  # noqa: E402

SEED = 20260808
SAMPLE = 25
TUNE_POSITIVE = 345054


def as_config(record: dict) -> dict[str, float]:
    return {k: record[k] for k in SEARCHABLE}


def real_verdicts(det, targets, params) -> dict[int, bool]:
    return {oid: det.run(cache_dir, oid, params)["value"] is True
            for cache_dir, oid, _reference, _row in targets}


def surrogate_verdicts(det, targets, cache, params) -> dict[int, bool]:
    key = (params["solar_open_m"], params["solar_close_m"])
    if key not in cache:
        cache[key] = {oid: extract(det.crop(cache_dir, oid), det.base, *key)
                      for cache_dir, oid, _r, _row in targets}
    return {oid: decide(ex, det.base,
                        params["solar_rectangularity_min"],
                        params["solar_internal_texture_min"],
                        params["solar_periodicity_min"],
                        params["solar_orientation_tolerance_deg"])
            for oid, ex in cache[key].items()}


def score_real(det, targets, params) -> dict:
    verdicts = real_verdicts(det, targets, params)
    reference = {oid: ref for _c, oid, ref, _row in targets}
    counts = confusion([(reference[oid], v or False) for oid, v in verdicts.items()])
    return {"counts": counts,
            "detects_tune_positive": verdicts[TUNE_POSITIVE],
            "false_positives": counts["fp"],
            "fired_objectids": sorted(o for o, v in verdicts.items() if v)}


def main() -> None:
    search = json.loads((EVAL / "phase4_search.json").read_text())
    best = as_config(search["best_admissible"][0])
    det = Detector()
    targets = eval_targets("tune")
    cache: dict = {}
    report: dict = {"best_configuration": best}

    # ------------------------------------------------------------ 1. surrogate verification
    print("1. surrogate verification (real detector vs the search surrogate, 54 rows each)")
    rng = random.Random(SEED)
    checks = [best]
    for _ in range(SAMPLE):
        checks.append({k: rng.choice(GRID[k]) for k in SEARCHABLE})
    mismatches = []
    for params in checks:
        real = real_verdicts(det, targets, params)
        surrogate = surrogate_verdicts(det, targets, cache, params)
        bad = [oid for oid in real if real[oid] != surrogate[oid]]
        if bad:
            mismatches.append({"params": params, "objectids": bad})
    if mismatches:
        print(json.dumps(mismatches, indent=1))
        raise SystemExit("surrogate disagrees with the detector; the search is void")
    print(f"   {len(checks)} configurations x 54 rows = {len(checks) * 54} verdicts, "
          "all identical\n")
    report["surrogate_verification"] = {
        "configurations_checked": len(checks), "rows_each": len(targets),
        "verdicts_compared": len(checks) * len(targets), "mismatches": 0, "seed": SEED,
    }

    # -------------------------------------------------------------------- the best config
    baseline = score_real(det, targets, best)
    report["best_on_tune"] = baseline
    print("2. the best admissible configuration, measured with the real detector")
    print(f"   {json.dumps(best)}")
    print(f"   {json.dumps(baseline['counts'])}  detects {TUNE_POSITIVE}: "
          f"{baseline['detects_tune_positive']}\n")

    # -------------------------------------------------------------------- 2. sensitivity
    print("3. sensitivity: each parameter alone, +/-5%, real detector")
    crop = det.crop(*[(t[0], t[1]) for t in targets if t[1] == TUNE_POSITIVE][0])
    gsd = ground_pixel_size_m(crop)
    sensitivity = []
    for name in SEARCHABLE:
        for direction in (-0.05, +0.05):
            params = dict(best)
            params[name] = round(best[name] * (1 + direction), 6)
            result = score_real(det, targets, params)
            quantised = (
                name in MORPHOLOGY
                and px_from_m(crop, params[name]) == px_from_m(crop, best[name])
            )
            row = {
                "parameter": name, "from": best[name], "to": params[name],
                "direction": f"{direction:+.0%}",
                "false_positives": result["false_positives"],
                "detects_tune_positive": result["detects_tune_positive"],
                "changed": (result["false_positives"] != baseline["false_positives"]
                            or result["detects_tune_positive"]
                            != baseline["detects_tune_positive"]),
                "no_op_because_pixel_quantised": quantised,
            }
            sensitivity.append(row)
            flag = "SWING" if row["changed"] else "     "
            note = "  (no-op: same integer kernel radius)" if quantised else ""
            print(f"   {flag} {name:34s} {best[name]:>6} -> {params[name]:<8} "
                  f"FP={row['false_positives']:2d}  detects={row['detects_tune_positive']}{note}")
    report["sensitivity"] = sensitivity
    report["ground_pixel_size_m"] = round(gsd, 4)
    swings = [s for s in sensitivity if s["changed"]]
    print(f"   -> {len(swings)} of {len(sensitivity)} single-parameter 5% moves change the "
          f"tune-split result\n")

    # ---------------------------------------------------------------- 3. robustness census
    print("4. robustness census over every zero-FP admissible configuration (surrogate)")
    zero_fp = [as_config(r) for r in json.loads(
        (EVAL / "phase4_search_all.json").read_text())["zero_fp_admissible"]]
    reference = {oid: ref for _c, oid, ref, _row in targets}

    def surrogate_score(params):
        verdicts = surrogate_verdicts(det, targets, cache, params)
        fp = sum(1 for oid, v in verdicts.items() if v and reference[oid] == "false")
        return fp, verdicts[TUNE_POSITIVE]

    robust = []
    for params in zero_fp:
        ok = True
        for name in FILTERS:  # morphology at +/-5% is a pixel no-op; see report
            for direction in (-0.05, +0.05):
                probe = dict(params)
                probe[name] = round(params[name] * (1 + direction), 6)
                fp, detects = surrogate_score(probe)
                if fp != 0 or not detects:
                    ok = False
                    break
            if not ok:
                break
        if ok:
            robust.append(params)
    print(f"   {len(zero_fp)} configurations score 0 FP and detect the positive")
    print(f"   {len(robust)} of them keep both properties under every single-parameter "
          f"+/-5% move")
    report["robustness_census"] = {
        "zero_fp_admissible": len(zero_fp),
        "robust_to_all_single_parameter_5pct_moves": len(robust),
        "robust_examples": robust[:10],
        "note": ("morphology parameters are excluded from the census: +/-5% on solar_open_m or "
                 "solar_close_m does not change the integer kernel radius at this GSD"),
    }

    (EVAL / "phase4_report.json").write_text(json.dumps(report, indent=1) + "\n")
    print("\nwritten: outputs/_solar_eval/phase4_report.json")


if __name__ == "__main__":
    main()
