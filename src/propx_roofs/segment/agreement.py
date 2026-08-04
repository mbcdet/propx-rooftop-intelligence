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
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
import shapely
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


def _repair(geom: BaseGeometry) -> BaseGeometry:
    """Make a polygon usable for set operations without changing its intent.

    ``approxPolyDP`` can occasionally emit a self-touching ring; ``buffer(0)`` resolves it. If the
    result is not polygonal the caller treats the candidate as a failure rather than measuring
    against something that is no longer an outline.
    """
    return geom if geom.is_valid else geom.buffer(0)


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
    """
    metric_crs = str(threshold(cfg, "crs", "metric"))
    if cv_polygon_wgs84 is None:
        return CvDelineation(
            segmenter=SEGMENTER_NAME,
            polygon=None,
            iou=None,
            symmetric_difference_ratio=None,
            hausdorff_m=None,
            boundary_gradient_ratio=None,
            alignment_warning=None,
            failure_reason=failure_reason or "segmentation_did_not_converge",
        )

    cv_metric, _ = to_metric(cv_polygon_wgs84, metric_crs)
    auth_metric, auth_measure = to_metric(authoritative_polygon_wgs84, metric_crs)
    cv_metric = _repair(cv_metric)
    auth_metric = _repair(auth_metric)
    if cv_metric.is_empty or "Polygon" not in cv_metric.geom_type:
        return CvDelineation(
            segmenter=SEGMENTER_NAME,
            polygon=None,
            iou=None,
            symmetric_difference_ratio=None,
            hausdorff_m=None,
            boundary_gradient_ratio=None,
            alignment_warning=None,
            failure_reason="cv_polygon_not_a_valid_outline",
        )

    union = cv_metric.union(auth_metric).area
    iou = float(cv_metric.intersection(auth_metric).area / union) if union > 0 else None
    sym_ratio = (
        float(cv_metric.symmetric_difference(auth_metric).area / auth_measure.area_m2)
        if auth_measure.area_m2 > 0
        else None
    )
    hausdorff = float(
        shapely.hausdorff_distance(
            cv_metric.boundary, auth_metric.boundary, densify=param(cfg, "hausdorff_densify")
        )
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
        alignment_warning=alignment_warning(iou, hausdorff, sym_ratio, cfg),
        failure_reason=None,
    )


def alignment_warning(
    iou: float | None, hausdorff_m: float | None, sym_ratio: float | None, cfg: Any
) -> dict[str, Any]:
    """The cautious ``boundary_alignment_warning`` of design 4.2.

    It fires only on material divergence — low agreement *or* a large worst-case separation — and
    its note is the design's wording verbatim: a flag for human inspection that makes **no claim
    that the authoritative data is incorrect or outdated**. When it does not fire, metrics and
    note are null, matching the output sketch in design 8.
    """
    iou_limit = param(cfg, "warn_iou_below")
    hausdorff_limit = param(cfg, "warn_hausdorff_above_m")
    reasons: list[str] = []
    if iou is not None and iou < iou_limit:
        reasons.append(f"iou {iou:.3f} below {iou_limit}")
    if hausdorff_m is not None and hausdorff_m > hausdorff_limit:
        reasons.append(f"hausdorff {hausdorff_m:.2f} m above {hausdorff_limit} m")
    if not reasons:
        return {"flag": False, "metrics": None, "note": None}
    return {
        "flag": True,
        "status": "requires_visual_review",
        "metrics": {
            "iou": None if iou is None else round(iou, 4),
            "hausdorff_m": None if hausdorff_m is None else round(hausdorff_m, 3),
            "symmetric_difference_ratio": None if sym_ratio is None else round(sym_ratio, 4),
            "triggers": reasons,
            "thresholds": {"iou_below": iou_limit, "hausdorff_above_m": hausdorff_limit},
        },
        "note": ALIGNMENT_NOTE,
    }
