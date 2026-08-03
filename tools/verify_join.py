"""Verify the 2025 roof-record to FMZK building-unit relationship using geometry.

Record counts cannot distinguish a genuine 1:1 join from a coincidence: a WFS bbox query
truncates any unit extending outside the box, so two totals can agree while describing
different sets. This script compares actual polygons instead. For each roof record it finds
the FMZK parts that overlap it, groups them by ``BW_GEB_ID``, takes the best-matching
dissolved unit, and reports IoU plus both containment ratios. Records and units touching the
bbox edge are reported separately so edge effects can be separated from real disagreement.

Run after ``make recon``:

    python3 tools/verify_join.py
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import shapely.affinity as affinity
from shapely.geometry import box, shape
from shapely.ops import unary_union

REPO_ROOT = Path(__file__).resolve().parents[1]
RECON_ROOT = REPO_ROOT / "outputs" / "recon"

IDENTITY_IOU = 0.99  # same geometry, vertex for vertex
SAME_BUILDING_IOU = 0.80  # same building, differently cut outline
M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON_EQUATOR = 111320.0


def to_metric(geometry, lat0: float):
    """Scale lon/lat degrees into an approximately isotropic local metric frame.

    IoU is a ratio, but raw degrees are anisotropic by ~1.5x at 48 deg N, which would
    distort every overlap measurement. Areas from this frame are in square metres.
    """
    return affinity.scale(
        geometry,
        xfact=math.cos(math.radians(lat0)) * M_PER_DEG_LON_EQUATOR,
        yfact=M_PER_DEG_LAT,
        origin=(0, 0),
    )


def analyse(area_dir: Path) -> dict[str, Any]:
    stats = json.loads((area_dir / "stats.json").read_text())
    bbox = stats["bbox_wgs84"]
    lat0 = (bbox[1] + bbox[3]) / 2
    bbox_poly = to_metric(box(*bbox), lat0)

    cache = area_dir / "cache"
    roofs = json.loads((cache / "roof_records_2025.geojson").read_text())["features"]
    parts = json.loads((cache / "fmzk_parts.geojson").read_text())["features"]

    by_unit: dict[Any, list] = defaultdict(list)
    null_parts = 0
    for part in parts:
        geometry = part.get("geometry")
        if not geometry:
            continue
        unit_id = part["properties"].get("BW_GEB_ID")
        if unit_id is None:
            null_parts += 1
            continue
        by_unit[unit_id].append(to_metric(shape(geometry), lat0))

    part_counts = Counter({unit_id: len(v) for unit_id, v in by_unit.items()})
    unit_geoms = {unit_id: unary_union(v) for unit_id, v in by_unit.items()}
    unit_clipped = {uid: not bbox_poly.contains(g) for uid, g in unit_geoms.items()}

    records = []
    for roof in roofs:
        geometry = roof.get("geometry")
        if not geometry:
            continue
        roof_geom = to_metric(shape(geometry), lat0)
        best_id, best = None, (0.0, 0.0, 0.0)
        for unit_id, unit_geom in unit_geoms.items():
            if not roof_geom.intersects(unit_geom):
                continue
            inter = roof_geom.intersection(unit_geom).area
            if inter <= 0:
                continue
            iou = inter / (roof_geom.area + unit_geom.area - inter)
            if iou > best[0]:
                best_id = unit_id
                best = (iou, inter / roof_geom.area, inter / unit_geom.area)
        props = roof["properties"]
        records.append(
            {
                "oid": props.get("OBJECTID"),
                "adr": props.get("ADRESSE"),
                "df": props.get("DACHFORM"),
                "slope": props.get("SLOPE_MEAN"),
                "m2": round(roof_geom.area, 1),
                "uid": best_id,
                "iou": round(best[0], 3),
                "cov_roof": round(best[1], 3),
                "cov_unit": round(best[2], 3),
                "roof_clip": not bbox_poly.contains(roof_geom),
                "unit_clip": unit_clipped.get(best_id, False),
                "parts": part_counts.get(best_id, 0),
            }
        )

    interior = [r for r in records if not r["roof_clip"] and not r["unit_clip"]]
    claims = Counter(r["uid"] for r in records if r["uid"] is not None)

    def band(rows: list[dict[str, Any]], lo: float, hi: float) -> int:
        return sum(1 for r in rows if lo <= r["iou"] < hi)

    def median(values: list[float]) -> float | None:
        return sorted(values)[len(values) // 2] if values else None

    return {
        "area": stats["area"],
        "bbox_wgs84": bbox,
        "n_roof_records": len(records),
        "n_units_with_id": len(unit_geoms),
        "n_parts_null_bw_geb_id": null_parts,
        "roof_records_clipped_by_bbox": sum(r["roof_clip"] for r in records),
        "matched_units_truncated_by_bbox": sum(r["unit_clip"] for r in records),
        "n_interior": len(interior),
        "interior_bands": {
            "iou_ge_0.99": band(interior, IDENTITY_IOU, 1.01),
            "iou_0.95_0.99": band(interior, 0.95, IDENTITY_IOU),
            "iou_0.90_0.95": band(interior, 0.90, 0.95),
            "iou_0.80_0.90": band(interior, SAME_BUILDING_IOU, 0.90),
            "iou_below_0.80": band(interior, 0.0, SAME_BUILDING_IOU),
        },
        "interior_median_cov_roof_in_unit": median([r["cov_roof"] for r in interior]),
        "interior_median_cov_unit_in_roof": median([r["cov_unit"] for r in interior]),
        "units_with_multiple_roof_records": {k: v for k, v in claims.items() if v > 1},
        "n_units_with_no_roof_record": len(set(unit_geoms) - set(claims)),
        "largest_units_by_part_count": [
            {
                "bw_geb_id": uid,
                "parts": n,
                "dissolved_m2": round(unit_geoms[uid].area, 1),
                "bbox_truncated": unit_clipped[uid],
                "claimed_by": [
                    {"oid": r["oid"], "iou": r["iou"], "adr": r["adr"]}
                    for r in records
                    if r["uid"] == uid
                ],
            }
            for uid, n in part_counts.most_common(3)
        ],
        "records": records,
    }


def main() -> int:
    summary = {}
    areas = sorted(p for p in RECON_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_"))
    for area_dir in areas:
        result = analyse(area_dir)
        summary[result["area"]] = result
        (area_dir / "join_records.json").write_text(
            json.dumps(result["records"], indent=1) + "\n", encoding="utf-8"
        )
        bands = result["interior_bands"]
        print(f"\n=== {result['area']} ===")
        print(
            f"  roof records {result['n_roof_records']:4d} | units {result['n_units_with_id']:4d}"
            f" | null-id parts {result['n_parts_null_bw_geb_id']:4d}"
        )
        print(
            f"  clipped: roofs {result['roof_records_clipped_by_bbox']:3d}"
            f"  matched units {result['matched_units_truncated_by_bbox']:3d}"
            f"  -> interior n={result['n_interior']}"
        )
        for label, count in bands.items():
            print(f"    interior {label:<15} {count:4d}")
        print(
            f"  interior median containment: roof-in-unit"
            f" {result['interior_median_cov_roof_in_unit']}"
            f"  unit-in-roof {result['interior_median_cov_unit_in_roof']}"
        )
        print(f"  units with >1 roof record: {result['units_with_multiple_roof_records']}")
        print(f"  units with no roof record: {result['n_units_with_no_roof_record']}")
        for unit in result["largest_units_by_part_count"][:1]:
            print(
                f"  largest unit {unit['bw_geb_id']}: {unit['parts']} parts,"
                f" {unit['dissolved_m2']:,.0f} m2, truncated={unit['bbox_truncated']},"
                f" claimed by {len(unit['claimed_by'])} record(s) {unit['claimed_by'][:1]}"
            )

    out = RECON_ROOT / "join_verification.json"
    out.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
