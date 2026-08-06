"""Phase 4: search the six permitted solar parameters on the TUNE SPLIT ONLY.

Objective, fixed by `preregistration.md` §2.3: among configurations that **detect the tune
positive 345054**, minimise false positives on the 53 tune negatives. Never minimise the
false-positive count alone — a configuration that never fires scores zero and is disqualified
by §2.2.

**Why there is a surrogate.** Two of the six parameters (``solar_open_m``, ``solar_close_m``) are
morphological and change which connected components exist; the other four are filters applied to
components that already exist. So components are extracted once per (open, close) pair per roof —
using the *same* helper functions ``solar.py`` itself calls, imported, not reimplemented — and the
four filter parameters are then swept in arithmetic. Nothing is decided on the surrogate: every
configuration that survives the sweep is re-run through the real ``observe_solar_panels`` and the
two verdicts must agree on all 54 rows or the run aborts. The surrogate prunes; the detector
decides.

The held-out split is never loaded here. There is no code path in this file that can read it.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import cv2
import numpy as np

EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL.parents[1] / "src"))
sys.path.insert(0, str(EVAL))

from score import SEARCHABLE, Detector, eval_targets  # noqa: E402

from propx_roofs.attributes import ATTRIBUTE_PARAMS  # noqa: E402
from propx_roofs.attributes.solar import (  # noqa: E402
    _angle_spread,
    _ellipse,
    _internal_texture,
    _row_periodicity,
)
from propx_roofs.imaging import no_data_mask, pixel_area_m2, px_from_m, threshold  # noqa: E402

TUNE_POSITIVE = 345054

# The search space. Each list brackets the shipped value (marked *) on both sides.
GRID = {
    # 345054's array reads as one 652 m2 L-shaped cluster at rectangularity 0.226, so anything
    # above ~0.23 cannot admit it; the range runs well below the shipped 0.55 to find out what
    # admitting it costs.
    "solar_rectangularity_min": [0.15, 0.20, 0.22, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65],  # noqa: E501
    "solar_periodicity_min": [0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30],
    "solar_internal_texture_min": [15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0],
    "solar_orientation_tolerance_deg": [5.0, 10.0, 15.0, 20.0, 25.0, 30.0],
    "solar_open_m": [0.2, 0.3, 0.4, 0.5],
    "solar_close_m": [0.3, 0.5, 0.7, 1.0],
}
MORPHOLOGY = ("solar_open_m", "solar_close_m")
FILTERS = ("solar_rectangularity_min", "solar_internal_texture_min",
           "solar_periodicity_min", "solar_orientation_tolerance_deg")


def extract(crop, cfg, open_m: float, close_m: float) -> dict:
    """Every candidate component on one roof, unrounded, before any filter parameter applies.

    A transcription of the extraction half of ``observe_solar_panels`` that calls the same
    helpers. It stops where the filters begin; it makes no detection decision.
    """
    max_value = float(threshold(cfg, "image", "solar_panels", "max_value"))
    max_saturation = float(threshold(cfg, "image", "solar_panels", "max_saturation"))
    min_cluster_m2 = float(threshold(cfg, "image", "solar_panels", "min_cluster_area_m2"))

    roof = np.asarray(crop.roof_mask, dtype=bool) & ~no_data_mask(crop)
    roof_px = int(roof.sum())
    if roof_px == 0:
        return {"roof_px": 0, "clusters": []}

    hsv = cv2.cvtColor(crop.rgb, cv2.COLOR_RGB2HSV)
    candidate = roof & (hsv[..., 2] <= max_value) & (hsv[..., 1] <= max_saturation)
    cleaned = cv2.morphologyEx(
        candidate.astype(np.uint8) * 255, cv2.MORPH_OPEN, _ellipse(px_from_m(crop, open_m))
    )
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, _ellipse(px_from_m(crop, close_m)))

    pixel_m2 = pixel_area_m2(crop)
    min_cluster_px = int(round(min_cluster_m2 / pixel_m2))
    texture = _internal_texture(crop, cfg)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(cleaned, connectivity=8)
    clusters = []
    for label in range(1, count):
        area_px = int(stats[label, cv2.CC_STAT_AREA])
        if area_px < min_cluster_px:
            continue
        selected = labels == label
        component = selected.astype(np.uint8)
        contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        (_, _), (width, height), angle = cv2.minAreaRect(max(contours, key=cv2.contourArea))
        rect_area = width * height
        clusters.append({
            "area_px": area_px,
            "rectangularity": float(area_px / rect_area) if rect_area > 0 else 0.0,
            "angle": round(float(angle) % 90.0, 1),  # solar.py rounds before the spread
            "texture": float(texture[selected].mean()),
            "periodicity": round(_row_periodicity(component, float(angle)), 4),
        })
    return {"roof_px": roof_px, "clusters": clusters}


def decide(extracted: dict, cfg, rect_min, texture_min, periodicity_min, orientation_tol) -> bool:
    """The decision half of ``observe_solar_panels``, on already-extracted components."""
    min_coverage = float(threshold(cfg, "image", "solar_panels", "min_coverage_fraction"))
    if not extracted["roof_px"]:
        return False
    kept = [c for c in extracted["clusters"]
            if c["rectangularity"] >= rect_min and c["texture"] >= texture_min]
    coverage = sum(c["area_px"] for c in kept) / extracted["roof_px"]
    spread = _angle_spread([c["angle"] for c in kept])
    co_oriented = len(kept) >= 2 and spread is not None and spread <= orientation_tol
    periodic = max((c["periodicity"] for c in kept), default=0.0) >= periodicity_min
    return coverage >= min_coverage and (co_oriented or periodic)


def main() -> None:
    det = Detector()
    targets = eval_targets("tune")
    assert len(targets) == 54, len(targets)
    positives = [t for t in targets if t[2] == "true"]
    assert [t[1] for t in positives] == [TUNE_POSITIVE]

    print(f"tune split: {len(targets)} rows, {len(positives)} positive, "
          f"{len(targets) - len(positives)} negative")
    print("crops + component extraction over "
          f"{len(GRID['solar_open_m']) * len(GRID['solar_close_m'])} morphology settings ...")

    # ------------------------------------------------------------------ extraction
    extracted: dict[tuple[float, float], list] = {}
    for open_m, close_m in itertools.product(GRID["solar_open_m"], GRID["solar_close_m"]):
        rows = []
        for cache_dir, oid, reference, _row in targets:
            crop = det.crop(cache_dir, oid)
            rows.append((oid, reference, extract(crop, det.base, open_m, close_m)))
        extracted[(open_m, close_m)] = rows
        print(f"  open={open_m} close={close_m}: done")

    # ------------------------------------------------------------------ the sweep
    results = []
    filter_grid = list(itertools.product(*(GRID[k] for k in FILTERS)))
    for (open_m, close_m), rows in extracted.items():
        for rect_min, texture_min, periodicity_min, orientation_tol in filter_grid:
            fp, detected_positive = 0, False
            for _oid, reference, ex in rows:
                fired = decide(ex, det.base, rect_min, texture_min, periodicity_min,
                               orientation_tol)
                if reference == "true":
                    detected_positive = fired
                elif fired:
                    fp += 1
            results.append({
                "solar_rectangularity_min": rect_min,
                "solar_internal_texture_min": texture_min,
                "solar_periodicity_min": periodicity_min,
                "solar_orientation_tolerance_deg": orientation_tol,
                "solar_open_m": open_m,
                "solar_close_m": close_m,
                "detects_tune_positive": detected_positive,
                "false_positives": fp,
            })

    total = len(results)
    admissible = [r for r in results if r["detects_tune_positive"]]
    print(f"\nsearched {total} configurations; {len(admissible)} detect {TUNE_POSITIVE}")

    def distance(r):
        """Tie-break toward the shipped configuration (§2.3)."""
        return sum(abs(r[k] - ATTRIBUTE_PARAMS[k]) / abs(ATTRIBUTE_PARAMS[k]) for k in SEARCHABLE)

    admissible.sort(key=lambda r: (r["false_positives"], distance(r)))
    never_fires = [r for r in results
                   if not r["detects_tune_positive"] and r["false_positives"] == 0]

    out = {
        "grid": GRID,
        "configurations_searched": total,
        "detect_tune_positive": len(admissible),
        "degenerate_zero_fp_configurations": len(never_fires),
        "best_admissible": admissible[:20],
        "fp_distribution_admissible": {},
    }
    for r in admissible:
        key = str(r["false_positives"])
        out["fp_distribution_admissible"][key] = out["fp_distribution_admissible"].get(key, 0) + 1
    (EVAL / "phase4_search.json").write_text(json.dumps(out, indent=1) + "\n")
    # The full zero-false-positive set, for the robustness census in phase4_verify.py: the
    # question "is this a stable region or a knife edge" cannot be answered from one winner.
    (EVAL / "phase4_search_all.json").write_text(json.dumps(
        {"zero_fp_admissible": [r for r in admissible if r["false_positives"] == 0]}, indent=1
    ) + "\n")

    print(f"configurations scoring 0 FP *without* detecting the positive (degenerate): "
          f"{len(never_fires)}")
    if admissible:
        print("\nbest admissible (detects the positive), fewest false positives first:")
        for r in admissible[:10]:
            print("  FP={false_positives:2d}  rect={solar_rectangularity_min} "
                  "tex={solar_internal_texture_min} per={solar_periodicity_min} "
                  "orient={solar_orientation_tolerance_deg} open={solar_open_m} "
                  "close={solar_close_m}".format(**r))
        print("\nfalse-positive distribution among admissible configurations:")
        for k in sorted(out["fp_distribution_admissible"], key=int):
            print(f"  {k} FP: {out['fp_distribution_admissible'][k]} configurations")
    else:
        print("\nNO configuration in the searched space detects the tune positive.")


if __name__ == "__main__":
    main()
