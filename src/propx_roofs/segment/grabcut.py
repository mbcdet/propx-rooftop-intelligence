"""Footprint-seeded GrabCut plus geometric refinement — the approved baseline (design 4.3).

**This is cadastre-seeded refinement and QC, not independent detection.** The trimap that
initialises GrabCut is built from the authoritative outline, so the candidate polygon is a
*refinement of the seed*, not an independent re-detection of the roof: it can disagree with the
outline only within the trimap band, and every agreement metric downstream
(:mod:`propx_roofs.segment.agreement`) compares two statistically **dependent** estimates.
High agreement therefore means "the imagery did not contradict the cadastre near its
boundary", never "the cadastre was independently confirmed". Design 4 fixes this division of
labour — authoritative geodata delineates; CV provides consistency evidence — and this module
has no code path that returns the candidate as a primary outline.

OpenCV only. No deep learning, no model weights, no downloads: a segmenter that cannot run from
a committed cache on a laptop with no network is not reproducible, and SAM is explicitly a
post-baseline stretch item behind an opt-in flag (design 4.3).

Two properties are deliberate and tested:

* **Determinism, including under threads.** OpenCV's GrabCut initialises its GMMs with
  k-means++, which draws from the global OpenCV RNG, and ``cv2.setRNGSeed`` is
  **process-global** — a second thread seeding between another thread's seed and its
  ``grabCut`` call would silently change that call's result. All GrabCut work therefore runs
  behind a module-level lock, and the seed is (re)set *inside* the lock immediately before
  every call, so repeated and concurrent runs produce the same mask bit for bit. The lock
  serialises segmentation within one process; scaling out is done with multiple *processes*
  (each with its own OpenCV RNG), which is the documented parallelism path.
* **Failure is a value, not an exception.** A degenerate result returns ``(None, meta)`` with a
  ``failure_reason``. Design 4 treats a null ``cv_candidate_polygon`` as a reportable outcome —
  the authoritative outline is the primary geometry either way, so a failed candidate costs the
  record nothing and must not cost the run anything.
"""

from __future__ import annotations

import threading
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

# cv2.setRNGSeed is PROCESS-GLOBAL: concurrent per-building threads would trample each other's
# seeds between the seeding and the grabCut call, silently breaking determinism. Every
# seed-then-segment pair runs inside this lock; multi-process parallelism is the documented
# scale-out path (see the module docstring).
_GRABCUT_LOCK = threading.Lock()


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


def _components_near_seed(
    mask_u8: np.ndarray, near: np.ndarray
) -> tuple[np.ndarray | None, int, int]:
    """Keep the 8-connected components that touch ``near``: ``(mask, n_kept, n_dropped)``.

    A MultiPolygon roof record is several surfaces, so "keep the largest component" (the
    previous rule) silently discarded every other part while the area gate could still pass.
    Instead, every foreground component that intersects the dilated authoritative outline is
    retained, and only components touching no input part — a co-coloured neighbouring roof, a
    patch of pavement — are dropped as noise. ``None`` when nothing survives.
    """
    count, labels, _, _ = cv2.connectedComponentsWithStats(mask_u8, connectivity=8)
    kept = np.zeros(mask_u8.shape, dtype=bool)
    n_kept = n_dropped = 0
    for label in range(1, count):
        component = labels == label
        if (component & near).any():
            kept |= component
            n_kept += 1
        else:
            n_dropped += 1
    if n_kept == 0:
        return None, 0, n_dropped
    return kept.astype(np.uint8) * 255, n_kept, n_dropped


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
    labels = trimap.copy()
    # setRNGSeed is process-global, so the seed-then-segment pair is atomic under the lock:
    # another thread must not be able to reseed between these two lines (see module docstring).
    with _GRABCUT_LOCK:
        cv2.setRNGSeed(rng_seed)  # k-means++ inside initGMMs draws from the global RNG
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

    # Input components of the authoritative outline (a MultiPolygon record has several), so the
    # output can be checked against them part by part rather than only by total area.
    n_input, seed_labels = cv2.connectedComponents(seed.astype(np.uint8), connectivity=8)
    n_input -= 1  # label 0 is background
    meta["n_input_components"] = n_input

    dilated_seed = cv2.dilate(
        seed.astype(np.uint8), _ellipse(px_from_m(crop, param(cfg, "trimap_dilate_m")))
    ) > 0
    kept, n_kept, n_dropped = _components_near_seed(foreground, dilated_seed)
    meta["n_foreground_components_kept"] = n_kept
    meta["n_noise_components_removed"] = n_dropped
    if kept is None:
        meta["failure_reason"] = "empty_segmentation"
        return None, meta

    closed = cv2.morphologyEx(
        kept, cv2.MORPH_CLOSE, _ellipse(px_from_m(crop, param(cfg, "close_m")))
    )
    # RETR_CCOMP so interior rings survive: the immediate children of each outer contour are the
    # courtyard voids, which must be punched out rather than filled (design 1.3, 3.2).
    contours, hierarchy = cv2.findContours(closed, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if not contours or hierarchy is None:
        meta["failure_reason"] = "no_contour_after_refinement"
        return None, meta
    tree = hierarchy[0]
    min_hole_px = param(cfg, "min_hole_area_m2") / pixel_area_m2(crop)
    epsilon_px = simplify_m / ground_pixel_size_m(crop)

    # Every outer contour is a candidate part: a MultiPolygon roof keeps all its parts. A part
    # that simplifies below three vertices is dropped as degenerate; losing every part is the
    # failure it always was.
    parts: list[tuple[np.ndarray, list[np.ndarray]]] = []
    for outer_index in (i for i in range(len(contours)) if tree[i][3] < 0):
        approx = cv2.approxPolyDP(contours[outer_index], epsilon_px, True)
        if len(approx) < 3:
            continue
        holes = [
            cv2.approxPolyDP(contours[i], epsilon_px, True)
            for i in range(len(contours))
            if tree[i][3] == outer_index and cv2.contourArea(contours[i]) >= min_hole_px
        ]
        parts.append((approx, [h for h in holes if len(h) >= 3]))
    meta["n_vertices"] = int(sum(len(approx) for approx, _ in parts))
    meta["n_interior_rings"] = int(sum(len(holes) for _, holes in parts))
    meta["n_output_components"] = len(parts)
    if not parts:
        meta["failure_reason"] = "degenerate_contour_after_simplification"
        return None, meta

    refined = np.zeros(seed.shape, dtype=np.uint8)
    cv2.fillPoly(refined, [approx.reshape(-1, 2) for approx, _ in parts], 255)
    all_holes = [h.reshape(-1, 2) for _, holes in parts for h in holes]
    if all_holes:
        cv2.fillPoly(refined, all_holes, 0)
    mask = refined.astype(bool)

    # Per-input-component recall: the share of each authoritative part the candidate covers.
    # This is what makes a lost MultiPolygon part *visible* — the whole-roof area ratio can sit
    # comfortably inside its band while one part of several has vanished entirely.
    recalls = [
        round(float((mask & part).sum() / part.sum()), 4)
        for label in range(1, n_input + 1)
        for part in [seed_labels == label]
    ]
    meta["input_component_recall"] = recalls
    lost = [i for i, recall in enumerate(recalls) if recall < 0.5]
    if n_input > 1:
        meta["multipolygon_input"] = True
        if lost:
            meta["component_count_warning"] = (
                f"{len(lost)} of {n_input} authoritative outline components are less than "
                f"half covered by the candidate (component recalls: {recalls}); the candidate "
                f"is degraded evidence for those parts even where the total area ratio passes"
            )

    # Both counts come from the same crop, so the Web Mercator scale factor cancels and this is
    # a true area ratio without any projection step.
    area_ratio = float(mask.sum()) / float(seed_px)
    meta["area_ratio_vs_authoritative"] = round(area_ratio, 4)
    if not param(cfg, "area_ratio_min") <= area_ratio <= param(cfg, "area_ratio_max"):
        meta["failure_reason"] = "area_ratio_outside_sane_band"
        return None, meta

    polygons = [rings_to_polygon_wgs84(crop, approx, holes) for approx, holes in parts]
    polygons = [p for p in polygons if p is not None]
    if not polygons:
        meta["failure_reason"] = "contour_not_convertible_to_polygon"
        return None, meta
    meta["polygon"] = (
        polygons[0]
        if len(polygons) == 1
        else {
            "type": "MultiPolygon",
            "coordinates": [p["coordinates"] for p in polygons],
        }
    )
    # The largest part's exterior, for the overlay debug path; the full geometry is `polygon`.
    largest = max(parts, key=lambda part: cv2.contourArea(part[0]))
    meta["contour_px"] = largest[0].reshape(-1, 2).astype(int).tolist()
    return mask, meta
