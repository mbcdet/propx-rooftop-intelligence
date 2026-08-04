"""Footprint-seeded GrabCut plus geometric refinement — the approved baseline (design 4.3).

OpenCV only. No deep learning, no model weights, no downloads: a segmenter that cannot run from
a committed cache on a laptop with no network is not reproducible, and SAM is explicitly a
post-baseline stretch item behind an opt-in flag (design 4.3).

Two properties are deliberate and tested:

* **Determinism.** OpenCV's GrabCut initialises its GMMs with k-means++, which draws from the
  global OpenCV RNG. Two calls in one process would therefore not be byte-identical. The seed is
  set immediately before every call and the iteration count is fixed, so repeated runs produce
  the same mask bit for bit.
* **Failure is a value, not an exception.** A degenerate result returns ``(None, meta)`` with a
  ``failure_reason``. Design 4 treats a null ``cv_candidate_polygon`` as a reportable outcome —
  the authoritative outline is the primary geometry either way, so a failed candidate costs the
  record nothing and must not cost the run anything.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from ..imaging import (
    ground_pixel_size_m,
    no_data_mask,
    pixel_area_m2,
    px_from_m,
    rings_to_polygon_wgs84,
    threshold,
)
from ..types import ImageCrop
from . import SEGMENTER_NAME, param


def _ellipse(radius_px: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius_px + 1, 2 * radius_px + 1))


def build_trimap(crop: ImageCrop, cfg: Any) -> np.ndarray:
    """GrabCut initialisation mask derived from the authoritative outline.

    Eroded outline -> ``GC_FGD`` (sure roof). Outside the dilated outline -> ``GC_BGD`` (sure
    background). The band between is probable, split on the outline itself: probably-foreground
    inside it, probably-background outside. Seeding the whole band as probably-foreground would
    bias the result outward by the dilation radius, which would then show up as a systematic
    positive area bias in the agreement numbers.
    """
    seed = np.asarray(crop.roof_mask, dtype=bool)
    seed_u8 = seed.astype(np.uint8)
    eroded = cv2.erode(seed_u8, _ellipse(px_from_m(crop, param(cfg, "trimap_erode_m"))))
    dilated = cv2.dilate(seed_u8, _ellipse(px_from_m(crop, param(cfg, "trimap_dilate_m"))))

    trimap = np.full(seed.shape, cv2.GC_BGD, dtype=np.uint8)
    trimap[(dilated > 0) & ~seed] = cv2.GC_PR_BGD
    trimap[seed] = cv2.GC_PR_FGD
    trimap[eroded > 0] = cv2.GC_FGD
    return trimap


def _largest_component(mask_u8: np.ndarray) -> np.ndarray | None:
    """The largest 8-connected component, or ``None`` if there is none.

    A roof is one connected surface. Keeping every component would let a co-coloured neighbouring
    roof or a patch of pavement join the candidate polygon.
    """
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask_u8, connectivity=8)
    if count <= 1:
        return None
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return (labels == largest).astype(np.uint8) * 255


def segment(crop: ImageCrop, cfg: Any) -> tuple[np.ndarray | None, dict[str, Any]]:
    """Segment the roof in ``crop``, seeded by its authoritative mask.

    Returns ``(mask, meta)``. ``mask`` is HxW bool, or ``None`` when the result is degenerate —
    in which case ``meta["failure_reason"]`` says why. ``meta`` always carries every parameter
    used, the observed area ratio, and (on success) the refined contour and its GeoJSON polygon
    in EPSG:4326.
    """
    iterations = int(param(cfg, "grabcut_iterations"))
    rng_seed = int(param(cfg, "grabcut_rng_seed"))
    simplify_m = param(cfg, "simplify_native_gsd_multiple") * float(
        threshold(cfg, "imagery", "native_gsd_m")
    )
    meta: dict[str, Any] = {
        "segmenter": SEGMENTER_NAME,
        "grabcut_iterations": iterations,
        "grabcut_rng_seed": rng_seed,
        "trimap_erode_m": param(cfg, "trimap_erode_m"),
        "trimap_dilate_m": param(cfg, "trimap_dilate_m"),
        "close_m": param(cfg, "close_m"),
        "simplify_tolerance_m": round(simplify_m, 3),
        "ground_pixel_size_m": round(ground_pixel_size_m(crop), 4),
        "missing_tiles": list(crop.missing_tiles),
        "area_ratio_band": [param(cfg, "area_ratio_min"), param(cfg, "area_ratio_max")],
        "failure_reason": None,
    }

    seed = np.asarray(crop.roof_mask, dtype=bool)
    seed_px = int(seed.sum())
    meta["authoritative_mask_px"] = seed_px
    if seed_px == 0:
        meta["failure_reason"] = "authoritative_mask_empty"
        return None, meta

    # A cache gap is uniform black, and GrabCut on a uniform region simply returns its
    # initialisation — which is the authoritative outline. That would produce a plausible-looking
    # IoU derived entirely from the seed, so it is refused instead.
    data_fraction = 1.0 - float((no_data_mask(crop) & seed).sum()) / seed_px
    meta["roof_data_fraction"] = round(data_fraction, 4)
    if data_fraction < param(cfg, "min_roof_data_fraction"):
        meta["failure_reason"] = "insufficient_image_data_over_roof"
        return None, meta

    trimap = build_trimap(crop, cfg)
    if not (trimap == cv2.GC_FGD).any():
        # The outline is thinner than twice the erosion radius — a sliver or a very small roof.
        meta["failure_reason"] = "sure_foreground_empty_after_erosion"
        return None, meta
    if not (trimap == cv2.GC_BGD).any():
        # The dilated outline fills the crop, so GrabCut has no background sample to model.
        meta["failure_reason"] = "sure_background_empty_after_dilation"
        return None, meta

    # Channel order is irrelevant to the colour GMMs, so the crop's RGB goes in unconverted.
    image = np.ascontiguousarray(crop.rgb)
    cv2.setRNGSeed(rng_seed)  # k-means++ inside initGMMs draws from the global RNG
    labels = trimap.copy()
    cv2.grabCut(
        image,
        labels,
        None,
        np.zeros((1, 65), np.float64),
        np.zeros((1, 65), np.float64),
        iterations,
        cv2.GC_INIT_WITH_MASK,
    )
    foreground = ((labels == cv2.GC_FGD) | (labels == cv2.GC_PR_FGD)).astype(np.uint8) * 255

    component = _largest_component(foreground)
    if component is None:
        meta["failure_reason"] = "empty_segmentation"
        return None, meta

    closed = cv2.morphologyEx(
        component, cv2.MORPH_CLOSE, _ellipse(px_from_m(crop, param(cfg, "close_m")))
    )
    # RETR_CCOMP so interior rings survive: the immediate children of the outer contour are the
    # courtyard voids, which must be punched out rather than filled (design 1.3, 3.2).
    contours, hierarchy = cv2.findContours(closed, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if not contours or hierarchy is None:
        meta["failure_reason"] = "no_contour_after_refinement"
        return None, meta
    tree = hierarchy[0]
    outer = [i for i in range(len(contours)) if tree[i][3] < 0]
    if not outer:
        meta["failure_reason"] = "no_contour_after_refinement"
        return None, meta
    outer_index = max(outer, key=lambda i: cv2.contourArea(contours[i]))
    min_hole_px = param(cfg, "min_hole_area_m2") / pixel_area_m2(crop)
    hole_indices = [
        i
        for i in range(len(contours))
        if tree[i][3] == outer_index and cv2.contourArea(contours[i]) >= min_hole_px
    ]

    epsilon_px = simplify_m / ground_pixel_size_m(crop)
    approx = cv2.approxPolyDP(contours[outer_index], epsilon_px, True)
    meta["n_vertices"] = int(len(approx))
    if len(approx) < 3:
        meta["failure_reason"] = "degenerate_contour_after_simplification"
        return None, meta
    holes = [cv2.approxPolyDP(contours[i], epsilon_px, True) for i in hole_indices]
    holes = [h for h in holes if len(h) >= 3]
    meta["n_interior_rings"] = len(holes)

    refined = np.zeros(seed.shape, dtype=np.uint8)
    cv2.fillPoly(refined, [approx.reshape(-1, 2)], 255)
    if holes:
        cv2.fillPoly(refined, [h.reshape(-1, 2) for h in holes], 0)
    mask = refined.astype(bool)

    # Both counts come from the same crop, so the Web Mercator scale factor cancels and this is
    # a true area ratio without any projection step.
    area_ratio = float(mask.sum()) / float(seed_px)
    meta["area_ratio_vs_authoritative"] = round(area_ratio, 4)
    if not param(cfg, "area_ratio_min") <= area_ratio <= param(cfg, "area_ratio_max"):
        meta["failure_reason"] = "area_ratio_outside_sane_band"
        return None, meta

    polygon = rings_to_polygon_wgs84(crop, approx, holes)
    if polygon is None:
        meta["failure_reason"] = "contour_not_convertible_to_polygon"
        return None, meta
    meta["polygon"] = polygon
    meta["contour_px"] = approx.reshape(-1, 2).astype(int).tolist()
    return mask, meta
