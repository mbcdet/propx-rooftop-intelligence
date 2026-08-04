"""Agreement between the CV candidate outline and the authoritative outline.

**The CV polygon never replaces the authoritative polygon.** Nothing in this module returns it as
primary; it produces a :class:`~propx_roofs.types.CvDelineation`, whose own ``as_dict`` states
that the candidate is comparison evidence. Because the candidate is *seeded* from the
authoritative outline (``segment/grabcut.py``), IoU and Hausdorff measure agreement between two
related estimates — not segmentation accuracy, and never labelled as accuracy (design 4.1).

All metric values are computed in the projected CRS through
:func:`propx_roofs.geometry.to_metric`, which is the repository's only route to a metre
(Amendment B3): Web Mercator area error at Vienna's latitude is ~2.2x and would silently corrupt
a Hausdorff distance the same way it corrupts an area.

**Edge disagreement is measured on exterior rings only.** ``shapely``'s ``.boundary`` of a
polygon with holes is a MultiLineString of the exterior ring *plus* every interior ring, so a
Hausdorff distance taken over it answers two questions at once: how far the two perimeters sit
apart, and whether the two outlines have the same number of rings. Those are different findings
and only the first one is what ``boundary_alignment_warning`` is about. The candidate segmenter
keeps interior rings only above ``min_hole_area_m2`` (4.0 m2), so an authoritative outline with a
1 m2 light shaft *cannot* be matched ring-for-ring by the candidate; measuring over ``.boundary``
turns that configured behaviour into a divergence figure — on vie-swv-007 it reads 5.705 m, of
which the perimeters contribute 1.476 m and the two unmatched light shafts the rest.

So this module publishes both, separately, and drives the warning from the exterior-only pair:

* ``iou`` / ``hausdorff_m`` / ``exterior_iou`` — perimeter agreement, holes excluded.
* ``hausdorff_full_boundary_m`` — the previous all-rings figure, retained unchanged so the
  earlier published number stays traceable rather than being silently replaced.
* ``topology_mismatch`` — ring counts and ring areas, as a **diagnostic**. It carries no
  ``requires_visual_review`` status and is routed nowhere: a mismatch is expected from the
  segmenter's own configuration and is not a statement about the roof or about the record.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
import shapely
from shapely.geometry import MultiLineString, Polygon
from shapely.geometry.base import BaseGeometry

from ..geometry import to_metric
from ..imaging import lonlat_to_pixel, luminance, px_from_m, threshold
from ..types import CvDelineation, ImageCrop
from . import SEGMENTER_NAME, param

# Verbatim from design section 4.2. It is a statement about our own agreement measurement.
ALIGNMENT_NOTE = (
    "The image-derived candidate boundary differs materially from the authoritative outline in "
    "this area. Possible causes include segmentation error, shadow or occlusion, roof overhang, "
    "or a difference in epoch between imagery and the roof record. This is a flag for human "
    "inspection and does not indicate that the authoritative data is incorrect or outdated."
)

# What the warning was measured on. Published inside the warning so a reader never has to guess
# whether a ring-count difference could have contributed to it.
ALIGNMENT_BASIS = "exterior_rings_only"


def _repair(geom: BaseGeometry) -> BaseGeometry:
    """Make a polygon usable for set operations without changing its intent.

    ``approxPolyDP`` can occasionally emit a self-touching ring; ``buffer(0)`` resolves it. If the
    result is not polygonal the caller treats the candidate as a failure rather than measuring
    against something that is no longer an outline.
    """
    return geom if geom.is_valid else geom.buffer(0)


def _parts(geom_metric: BaseGeometry) -> list[Polygon]:
    """Non-empty polygonal members of an already-projected geometry.

    Deliberately local rather than reaching for ``geometry._polygons``: the agreement block must
    not depend on a private name in another module, and the rule ("non-polygonal input
    contributes nothing") is one line.
    """
    if isinstance(geom_metric, Polygon):
        return [] if geom_metric.is_empty else [geom_metric]
    return [
        g
        for g in getattr(geom_metric, "geoms", ())
        if isinstance(g, Polygon) and not g.is_empty
    ]


def _exterior_rings(geom_metric: BaseGeometry) -> BaseGeometry:
    """The exterior ring of every part, as a single line geometry — interior rings dropped.

    This, not ``.boundary``, is what a perimeter-separation measurement may be taken over. For a
    MultiPolygon every part contributes its own exterior ring, so a genuinely multipart outline
    is still measured in full.
    """
    return MultiLineString([list(p.exterior.coords) for p in _parts(geom_metric)])


def _without_interior_rings(geom_metric: BaseGeometry) -> BaseGeometry:
    """``geom_metric`` with every hole filled: each part rebuilt from its exterior ring, unioned.

    The union matters for a MultiPolygon — overlapping filled parts would otherwise double-count
    their shared area in the denominator of an IoU.
    """
    return shapely.union_all([Polygon(p.exterior) for p in _parts(geom_metric)])


def _interior_ring_areas_m2(geom_metric: BaseGeometry) -> list[float]:
    """Area of each interior ring across all parts, largest first. Metres, because projected."""
    areas = [Polygon(ring).area for p in _parts(geom_metric) for ring in p.interiors]
    return sorted(areas, reverse=True)


def topology_mismatch(
    cv_metric: BaseGeometry, auth_metric: BaseGeometry, cfg: Any, metric_crs: str
) -> dict[str, Any]:
    """Interior-ring bookkeeping for the two outlines. **A diagnostic, not a review trigger.**

    ``flag`` is true when the two outlines carry a different number of interior rings. That is a
    fact worth publishing — a reader comparing areas needs to know one outline excludes voids the
    other does not — but it is not evidence of an error on either side, so it carries no
    ``requires_visual_review`` status and nothing routes it to ``review_flags``. The candidate
    segmenter drops holes below ``min_hole_area_m2`` by design (``segment/__init__.py``), which
    means a mismatch is the *expected* result wherever the authoritative outline records a small
    light shaft, and the note says so without implying either outline is the better one.

    Ring areas are listed, not just counted, so the note can be checked against the data: a
    reader can see for themselves whether the unmatched rings sit below the configured minimum.
    """
    auth_areas = _interior_ring_areas_m2(auth_metric)
    cv_areas = _interior_ring_areas_m2(cv_metric)
    min_hole_area_m2 = param(cfg, "min_hole_area_m2")
    if len(auth_areas) == len(cv_areas):
        note = (
            "The candidate and the authoritative outline carry the same number of interior "
            "rings. Reported for completeness; ring counts are a diagnostic and are not an "
            "input to boundary_alignment_warning."
        )
    else:
        note = (
            f"The two outlines carry different numbers of interior rings. The candidate "
            f"segmenter does not produce interior rings below its configured minimum hole area "
            f"({min_hole_area_m2} m2), so wherever the authoritative outline records a void "
            f"smaller than that, a mismatch is the expected consequence of that setting rather "
            f"than a finding about the roof. This is a diagnostic only: it does not indicate "
            f"that the authoritative data is incorrect or outdated, it does not indicate that "
            f"the candidate outline is the better one, and it does not on its own require "
            f"visual review. Perimeter agreement is measured on exterior rings "
            f"(exterior_iou, hausdorff_m) so that it is unaffected by this difference; "
            f"hausdorff_full_boundary_m includes interior rings and therefore is affected."
        )
    return {
        "flag": len(auth_areas) != len(cv_areas),
        "authoritative_interior_rings": len(auth_areas),
        "cv_interior_rings": len(cv_areas),
        "authoritative_interior_ring_areas_m2": [round(a, 2) for a in auth_areas],
        "cv_interior_ring_areas_m2": [round(a, 2) for a in cv_areas],
        "crs": metric_crs,
        "cv_min_hole_area_m2": min_hole_area_m2,
        "note": note,
    }


def _boundary_gradient(crop: ImageCrop, polygon_wgs84: Any, width_px: int) -> float | None:
    """Mean Sobel gradient magnitude of the crop sampled along one polygon boundary.

    Returns ``None`` when the boundary falls entirely outside the crop.
    """
    grey = luminance(crop).astype(np.float32)
    gx = cv2.Sobel(grey, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(grey, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(gx, gy)

    stencil = np.zeros(grey.shape, dtype=np.uint8)
    for ring in _rings(polygon_wgs84):
        pts = np.rint([lonlat_to_pixel(crop, lon, lat) for lon, lat in ring]).astype(np.int32)
        cv2.polylines(stencil, [pts], True, 255, thickness=width_px)
    sampled = stencil > 0
    if not sampled.any():
        return None
    return float(magnitude[sampled].mean())


def _rings(polygon_wgs84: Any) -> list[list[tuple[float, float]]]:
    """Exterior and interior rings of a GeoJSON Polygon/MultiPolygon or shapely geometry."""
    if isinstance(polygon_wgs84, BaseGeometry):
        geom = polygon_wgs84
        parts = [geom] if geom.geom_type == "Polygon" else list(getattr(geom, "geoms", []))
        rings = []
        for part in parts:
            if part.geom_type != "Polygon":
                continue
            rings.append([(x, y) for x, y, *_ in part.exterior.coords])
            rings.extend([(x, y) for x, y, *_ in r.coords] for r in part.interiors)
        return rings
    kind = polygon_wgs84.get("type")
    coords = polygon_wgs84["coordinates"]
    groups = [coords] if kind == "Polygon" else coords
    return [[(float(c[0]), float(c[1])) for c in ring] for group in groups for ring in group]


def boundary_gradient_ratio(
    crop: ImageCrop, cv_polygon_wgs84: Any, authoritative_polygon_wgs84: Any, cfg: Any
) -> float | None:
    """Mean image gradient along the CV boundary divided by the same along the authoritative one.

    **This is the only seed-independent signal in the agreement block.** IoU, symmetric
    difference and Hausdorff all compare the candidate with the outline it was seeded from, so
    they are bounded below by the seeding itself and cannot say whether either boundary sits on a
    real image edge. This ratio can: gradient magnitude is a property of the pixels alone.

    Above 1.0 means the CV boundary sits on stronger image structure than the authoritative one;
    below 1.0 the reverse. It is **reported as evidence and decides nothing** about the primary
    polygon (design 4.1) — a roof edge in shadow has a weak gradient however correctly it is
    drawn, so a low ratio is not an error and a high one is not a promotion.

    ``None`` when either boundary cannot be sampled or the denominator is zero (a uniform crop,
    e.g. an area filled from missing tiles).
    """
    width_px = px_from_m(crop, param(cfg, "boundary_sample_width_m"))
    cv_mean = _boundary_gradient(crop, cv_polygon_wgs84, width_px)
    auth_mean = _boundary_gradient(crop, authoritative_polygon_wgs84, width_px)
    if cv_mean is None or auth_mean is None or auth_mean <= 0.0:
        return None
    return cv_mean / auth_mean


def compare(
    cv_polygon_wgs84: Any | None,
    authoritative_polygon_wgs84: Any,
    crop: ImageCrop,
    cfg: Any,
    failure_reason: str | None = None,
) -> CvDelineation:
    """Measure agreement between the candidate and the authoritative outline.

    ``cv_polygon_wgs84`` may be ``None`` (segmentation did not converge); pass the segmenter's
    ``meta["failure_reason"]`` through and the result carries it with every metric null. That is
    a reportable outcome, not an error: the primary outline is authoritative either way.

    Two families of number come back, and the distinction is the point (see the module
    docstring): ``iou``, ``exterior_iou`` and ``hausdorff_m`` describe the perimeters with holes
    excluded and are what the warning is computed from; ``hausdorff_full_boundary_m`` and
    ``topology_mismatch`` describe the ring structure and decide nothing.
    """
    metric_crs = str(threshold(cfg, "crs", "metric"))
    if cv_polygon_wgs84 is None:
        return _null_delineation(failure_reason or "segmentation_did_not_converge")

    cv_metric, _ = to_metric(cv_polygon_wgs84, metric_crs)
    auth_metric, auth_measure = to_metric(authoritative_polygon_wgs84, metric_crs)
    cv_metric = _repair(cv_metric)
    auth_metric = _repair(auth_metric)
    if cv_metric.is_empty or "Polygon" not in cv_metric.geom_type:
        return _null_delineation("cv_polygon_not_a_valid_outline")

    union = cv_metric.union(auth_metric).area
    iou = float(cv_metric.intersection(auth_metric).area / union) if union > 0 else None
    sym_ratio = (
        float(cv_metric.symmetric_difference(auth_metric).area / auth_measure.area_m2)
        if auth_measure.area_m2 > 0
        else None
    )

    # Holes filled on both sides, so a courtyard the candidate could not reproduce cannot depress
    # the overlap. Published next to iou rather than instead of it: the difference between the two
    # is exactly the void area, which a reader may want to see.
    cv_solid = _without_interior_rings(cv_metric)
    auth_solid = _without_interior_rings(auth_metric)
    solid_union = cv_solid.union(auth_solid).area
    exterior_iou = (
        float(cv_solid.intersection(auth_solid).area / solid_union) if solid_union > 0 else None
    )

    densify = param(cfg, "hausdorff_densify")
    hausdorff = float(
        shapely.hausdorff_distance(
            _exterior_rings(cv_metric), _exterior_rings(auth_metric), densify=densify
        )
    )
    hausdorff_full = float(
        shapely.hausdorff_distance(cv_metric.boundary, auth_metric.boundary, densify=densify)
    )
    gradient_ratio = boundary_gradient_ratio(
        crop, cv_polygon_wgs84, authoritative_polygon_wgs84, cfg
    )

    return CvDelineation(
        segmenter=SEGMENTER_NAME,
        polygon=cv_polygon_wgs84,
        iou=None if iou is None else round(iou, 4),
        symmetric_difference_ratio=None if sym_ratio is None else round(sym_ratio, 4),
        hausdorff_m=round(hausdorff, 3),
        boundary_gradient_ratio=None if gradient_ratio is None else round(gradient_ratio, 4),
        alignment_warning=alignment_warning(exterior_iou, hausdorff, sym_ratio, cfg),
        failure_reason=None,
        exterior_iou=None if exterior_iou is None else round(exterior_iou, 4),
        hausdorff_full_boundary_m=round(hausdorff_full, 3),
        topology_mismatch=topology_mismatch(cv_metric, auth_metric, cfg, metric_crs),
    )


def _null_delineation(failure_reason: str) -> CvDelineation:
    """Every metric null, with the reason. No candidate means nothing was measured, not zero."""
    return CvDelineation(
        segmenter=SEGMENTER_NAME,
        polygon=None,
        iou=None,
        symmetric_difference_ratio=None,
        hausdorff_m=None,
        boundary_gradient_ratio=None,
        alignment_warning=None,
        failure_reason=failure_reason,
        exterior_iou=None,
        hausdorff_full_boundary_m=None,
        topology_mismatch=None,
    )


def alignment_warning(
    iou: float | None, hausdorff_m: float | None, sym_ratio: float | None, cfg: Any
) -> dict[str, Any]:
    """The cautious ``boundary_alignment_warning`` of design 4.2.

    It fires only on material divergence — low agreement *or* a large worst-case separation — and
    its note is the design's wording verbatim: a flag for human inspection that makes **no claim
    that the authoritative data is incorrect or outdated**. When it does not fire, metrics and
    note are null, matching the output sketch in design 8.

    **Both arguments must be the exterior-only figures** (``exterior_iou`` and the exterior-ring
    ``hausdorff_m`` from :func:`compare`). Passing the all-rings ``hausdorff_full_boundary_m``
    here would make the warning fire on a ring-count difference the segmenter is configured to
    produce, which is why ``ALIGNMENT_BASIS`` is published inside ``metrics``: what the warning
    was measured on is part of the warning. The two thresholds are unchanged.
    """
    iou_limit = param(cfg, "warn_iou_below")
    hausdorff_limit = param(cfg, "warn_hausdorff_above_m")
    reasons: list[str] = []
    if iou is not None and iou < iou_limit:
        reasons.append(f"exterior_iou {iou:.3f} below {iou_limit}")
    if hausdorff_m is not None and hausdorff_m > hausdorff_limit:
        reasons.append(f"exterior-ring hausdorff {hausdorff_m:.2f} m above {hausdorff_limit} m")
    if not reasons:
        return {"flag": False, "metrics": None, "note": None}
    return {
        "flag": True,
        "status": "requires_visual_review",
        "metrics": {
            "basis": ALIGNMENT_BASIS,
            "exterior_iou": None if iou is None else round(iou, 4),
            "hausdorff_m": None if hausdorff_m is None else round(hausdorff_m, 3),
            "symmetric_difference_ratio": None if sym_ratio is None else round(sym_ratio, 4),
            "triggers": reasons,
            "thresholds": {"iou_below": iou_limit, "hausdorff_above_m": hausdorff_limit},
        },
        "note": ALIGNMENT_NOTE,
    }
