"""Phase 2 reconnaissance: fetch real imagery and vectors for the candidate study areas.

Run this where the Vienna endpoints are reachable:

    make recon

For each candidate area it writes, under ``outputs/recon/<area>/``:

  mosaic.png          z20 Orthofoto 2024 mosaic for the area bbox
  mosaic.json         the exact affine transform of that mosaic (EPSG:3857)
  overlay.png         the mosaic with authoritative roof outlines drawn on top
  stats.json          per-area source statistics
  cache/              raw tiles and GeoJSON, reused on re-runs

It deliberately makes no selection decision. Choosing the study area and the 8-10
buildings requires looking at the mosaics.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from propx_roofs.sources import wfs, wmts  # noqa: E402

logger = logging.getLogger("recon")

# Candidate areas. Compact boxes, roughly 450 x 550 m.
CANDIDATE_AREAS: dict[str, tuple[float, float, float, float]] = {
    # (min_lon, min_lat, max_lon, max_lat)
    "sonnwendviertel": (16.3760, 48.1830, 16.3820, 48.1880),
    "karlsplatz_tu": (16.3660, 48.1960, 16.3720, 48.2010),
    "seestadt": (16.5030, 48.2230, 16.5090, 48.2280),
}

ZOOM = 20
# Above this, SLOPE_MEAN is unusual for a whole-roof mean and is flagged for visual review.
# It is NOT treated as invalid: steep mansards, towers, spires and narrow or unusual roof
# geometries genuinely produce high mean slopes. The threshold selects candidates to look at,
# nothing more. Whether any given value is wrong can only be decided from the imagery.
SLOPE_REVIEW_THRESHOLD_DEG = 60.0


def _summarise_roof_records(features: list[dict[str, Any]]) -> dict[str, Any]:
    """Roof-form mix, slope distribution, and data-quality flags."""
    forms: dict[str, int] = {}
    slopes: list[float] = []
    review: list[dict[str, Any]] = []
    missing_dachform = 0

    for feature in features:
        props = feature.get("properties", {})
        form = props.get("DACHFORM")
        if form is None:
            missing_dachform += 1
        forms[str(form)] = forms.get(str(form), 0) + 1

        slope = props.get("SLOPE_MEAN")
        if isinstance(slope, (int, float)):
            slopes.append(float(slope))
            if slope > SLOPE_REVIEW_THRESHOLD_DEG:
                review.append(
                    {
                        "objectid": props.get("OBJECTID"),
                        "slope_mean": slope,
                        "dachform": form,
                        "status": "requires_visual_review",
                    }
                )

    summary: dict[str, Any] = {
        "n_records": len(features),
        "dachform_counts": dict(sorted(forms.items(), key=lambda kv: -kv[1])),
        "missing_dachform": missing_dachform,
        "slope_outliers_requires_visual_review": {
            "threshold_deg": SLOPE_REVIEW_THRESHOLD_DEG,
            "note": "High mean slope is unusual but not impossible. These records are "
            "retained and reported; the threshold only selects candidates for visual "
            "inspection. No value is rejected on the threshold alone.",
            "records": review,
        },
    }
    if slopes:
        summary["slope_mean_deg"] = {
            "min": round(min(slopes), 2),
            "median": round(statistics.median(slopes), 2),
            "max": round(max(slopes), 2),
        }
    return summary


def _fallback_threshold_disagreement(features: list[dict[str, Any]]) -> dict[str, Any]:
    """How often the documented slope-threshold fallback disagrees with DACHFORM.

    Quantifies the cost of the fallback path so the README can state it rather than
    assert that thresholds are "close enough".
    """
    wrong = abstain = comparable = 0
    for feature in features:
        props = feature.get("properties", {})
        form, slope = props.get("DACHFORM"), props.get("SLOPE_MEAN")
        if form not in ("Flachdach", "Schraegdach") or not isinstance(slope, (int, float)):
            continue
        comparable += 1
        predicted = "Flachdach" if slope < 10 else ("Schraegdach" if slope > 15 else None)
        if predicted is None:
            abstain += 1
        elif predicted != form:
            wrong += 1
    return {
        "comparable_records": comparable,
        "threshold_disagrees_with_dachform": wrong,
        "threshold_would_abstain": abstain,
        "note": "Fallback rule: <10 deg flat, >15 deg pitched, otherwise abstain. "
        "Used only where no 2025 roof record exists.",
    }


def _draw_outlines(image, transform: wmts.MosaicTransform, collection: dict[str, Any]):
    """Draw authoritative roof outlines onto a copy of the mosaic."""
    from PIL import ImageDraw

    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)

    def rings(geometry: dict[str, Any] | None):
        if not geometry:
            return
        kind, coords = geometry.get("type"), geometry.get("coordinates") or []
        if kind == "Polygon":
            yield from coords
        elif kind == "MultiPolygon":
            for polygon in coords:
                yield from polygon

    for feature in collection.get("features", []):
        for ring in rings(feature.get("geometry")):
            points = [transform.pixel_of(lon, lat) for lon, lat in ring]
            if len(points) >= 2:
                draw.line(points + [points[0]], fill=(255, 235, 59), width=2)
    return canvas


def run_area(name: str, bbox: tuple[float, float, float, float], out_root: Path) -> dict[str, Any]:
    out_dir = out_root / name
    cache_dir = out_dir / "cache"
    out_dir.mkdir(parents=True, exist_ok=True)

    centre_lat = (bbox[1] + bbox[3]) / 2
    logger.info(
        "%s: bbox=%s effective resolution at z%d = %.4f m/px",
        name,
        bbox,
        ZOOM,
        wmts.resolution_at_latitude(ZOOM, centre_lat),
    )

    # --- imagery -------------------------------------------------------------------
    mosaic, transform = wmts.build_mosaic(bbox, ZOOM, cache_dir / "tiles")
    mosaic.save(out_dir / "mosaic.png")
    transform.to_json(out_dir / "mosaic.json")

    # --- vectors -------------------------------------------------------------------
    roof_records = wfs.fetch_features(wfs.LAYER_ROOF_RECORD_2025, bbox)
    wfs.cache_features(roof_records, cache_dir / "roof_records_2025.geojson")

    building_parts = wfs.fetch_features(wfs.LAYER_BUILDING_PARTS, bbox)
    wfs.cache_features(building_parts, cache_dir / "fmzk_parts.geojson")

    typology = wfs.fetch_features(wfs.LAYER_TYPOLOGY, bbox, require_non_empty=False)
    wfs.cache_features(typology, cache_dir / "typology.geojson")

    building_info = wfs.fetch_features(wfs.LAYER_BUILDING_INFO, bbox, require_non_empty=False)
    wfs.cache_features(building_info, cache_dir / "building_info.geojson")

    _draw_outlines(mosaic, transform, roof_records).save(out_dir / "overlay.png")

    # --- building units ------------------------------------------------------------
    unit_ids: dict[str, int] = {}
    for feature in building_parts.get("features", []):
        key = str(feature.get("properties", {}).get("BW_GEB_ID"))
        unit_ids[key] = unit_ids.get(key, 0) + 1
    null_units = unit_ids.pop("None", 0)

    stats: dict[str, Any] = {
        "area": name,
        "bbox_wgs84": list(bbox),
        "imagery": {
            "layer": transform.layer,
            "zoom": ZOOM,
            "nominal_pixel_size_m": round(transform.pixel_size, 4),
            "effective_ground_resolution_m": round(
                wmts.resolution_at_latitude(ZOOM, centre_lat), 4
            ),
            "native_source_gsd_m": 0.15,
            "mosaic_px": [transform.width, transform.height],
        },
        "roof_records_2025": _summarise_roof_records(roof_records.get("features", [])),
        "roof_type_fallback_check": _fallback_threshold_disagreement(
            roof_records.get("features", [])
        ),
        "fmzk": {
            "n_parts": len(building_parts.get("features", [])),
            "n_units_by_bw_geb_id": len(unit_ids),
            "parts_with_null_bw_geb_id": null_units,
            "max_parts_per_unit": max(unit_ids.values()) if unit_ids else 0,
        },
        "typology_features": len(typology.get("features", [])),
        "building_info_points": len(building_info.get("features", [])),
        "attribution": wfs.ATTRIBUTION,
    }
    (out_dir / "stats.json").write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    logger.info("%s: wrote mosaic, overlay and stats to %s", name, out_dir)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", choices=sorted(CANDIDATE_AREAS), action="append")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "outputs" / "recon")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s %(message)s")
    areas = args.area or sorted(CANDIDATE_AREAS)

    results, failures = [], []
    for name in areas:
        try:
            results.append(run_area(name, CANDIDATE_AREAS[name], args.out))
        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            logger.error("%s FAILED: %s: %s", name, type(error).__name__, error)
            failures.append({"area": name, "error": f"{type(error).__name__}: {error}"})

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "recon_summary.json").write_text(
        json.dumps({"areas": results, "failures": failures}, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\n{'area':<20} {'roofs':>6} {'flat':>5} {'pitch':>6} {'units':>6} {'parts':>6}")
    for stats in results:
        forms = stats["roof_records_2025"]["dachform_counts"]
        print(
            f"{stats['area']:<20} {stats['roof_records_2025']['n_records']:>6} "
            f"{forms.get('Flachdach', 0):>5} {forms.get('Schraegdach', 0):>6} "
            f"{stats['fmzk']['n_units_by_bw_geb_id']:>6} {stats['fmzk']['n_parts']:>6}"
        )
    for failure in failures:
        print(f"{failure['area']:<20} FAILED: {failure['error']}")
    print(f"\nMosaics and overlays: {args.out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
