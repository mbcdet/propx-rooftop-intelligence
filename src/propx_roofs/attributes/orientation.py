"""Two orientation observations that are never merged into one number.

``ridge_orientation_deg`` is *observed*: it exists only when a ridge line is actually detected in
the imagery, and is ``None`` otherwise. ``footprint_axis_orientation_deg`` is *derived*: the long
axis of the minimum rotated rectangle of the authoritative outline, which always exists and says
nothing about the roof structure.

They are separate fields with separate provenance (design 5) because they answer different
questions, and a reader who saw one number labelled "orientation" would reasonably assume the
roof has a ridge along it. On a flat roof the footprint axis is defined and the ridge is not; on a
hipped roof both exist and can differ. Merging them would manufacture a ridge from a rectangle.

Both azimuths are grid bearings in the projected CRS, folded to [0, 180) because a ridge is
undirected — see :mod:`propx_roofs.geometry`.
"""

from __future__ import annotations

import math
from typing import Any

import cv2
import numpy as np

from ..geometry import azimuth_deg, min_rotated_rect_axis, to_metric
from ..imaging import (
    ground_pixel_size_m,
    luminance,
    no_data_mask,
    pixel_to_lonlat,
    px_from_m,
    shadow_mask,
    threshold,
)
from ..types import ImageCrop, ImageObservation
from . import judgeable_mask, param

RIDGE_METHOD = "hough_ridge_with_plane_contrast_interior_shadow_and_axis_gates"
AXIS_METHOD = "minimum_rotated_rectangle_long_axis"

RIDGE_LIMITATIONS = (
    "a ridge is only reported when detected; absence is not evidence of a flat roof",
    "a long parapet, a rooftop plant row or a panel row can present as a line, which is why a "
    "brightness contrast between the two sides is required as well as line agreement",
    "a facade, deck or terrace edge follows the outline and a shadow boundary darkens one "
    "flank, which is why interior clearance and a shadow-flank test gate every candidate",
    "15 cm GSD: a short dormer ridge on a small roof may not reach the minimum length",
    "one dominant orientation only; a hipped or multi-ridge roof is reported by its strongest",
)

AXIS_LIMITATIONS = (
    "building/roof long axis from the authoritative outline; NOT a roof slope aspect",
    "on a near-square footprint the axis is close to arbitrary; the two side lengths are "
    "reported so the caller can see how weakly it is defined",
)


def observe_footprint_axis(roof_geom_wgs84: Any, cfg: Any) -> ImageObservation:
    """Long-axis azimuth of the authoritative outline, in the projected CRS.

    Derived from geometry alone — no pixels are read — so it is available even on a fully
    shadowed roof. ``None`` only if the outline is degenerate enough to have no rectangle.
    """
    metric_crs = str(threshold(cfg, "crs", "metric"))
    projected, measure = to_metric(roof_geom_wgs84, metric_crs)
    try:
        azimuth, long_side, short_side = min_rotated_rect_axis(projected)
    except ValueError as error:
        return ImageObservation(
            name="footprint_axis_orientation_deg",
            value=None,
            method=AXIS_METHOD,
            rationale=f"outline has no rotated-rectangle axis: {error}",
            quality={"crs": metric_crs, "area_m2": round(measure.area_m2, 2)},
            limitations=AXIS_LIMITATIONS,
        )
    elongation = long_side / short_side if short_side > 0 else None
    weak = elongation is not None and elongation < 1.1
    return ImageObservation(
        name="footprint_axis_orientation_deg",
        value=round(azimuth, 2),
        method=AXIS_METHOD,
        rationale=(
            f"long axis of the minimum rotated rectangle of the authoritative outline, "
            f"{long_side:.1f} m by {short_side:.1f} m, as a grid bearing in {metric_crs}"
            + (
                "; the footprint is nearly square, so this axis is weakly defined"
                if weak
                else ""
            )
        ),
        quality={
            "crs": metric_crs,
            "long_side_m": round(long_side, 2),
            "short_side_m": round(short_side, 2),
            "elongation": None if elongation is None else round(elongation, 3),
            "weakly_defined": weak,
            "n_interior_rings": measure.n_interior_rings,
        },
        limitations=AXIS_LIMITATIONS,
    )


def _hough_lines(crop: ImageCrop, interior: np.ndarray, cfg: Any) -> np.ndarray:
    """Probabilistic Hough segments inside the eroded roof interior.

    Canny's two thresholds come from the median brightness of the roof interior rather than fixed
    numbers, so a dark roof and a bright roof get comparable edge sensitivity.
    """
    grey = luminance(crop)
    values = grey[interior]
    median = float(np.median(values)) if values.size else 0.0
    lower = int(max(0, 0.66 * median))
    upper = int(min(255, 1.33 * median))
    edges = cv2.Canny(cv2.GaussianBlur(grey, (5, 5), 0), lower, upper)
    edges[~interior] = 0
    min_length = px_from_m(crop, param(cfg, "ridge_min_length_m"))
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180.0,
        # A segment needs roughly as many votes as it has pixels of length: this keeps the
        # threshold tied to the same physical minimum length rather than being a second knob.
        threshold=min_length,
        minLineLength=min_length,
        maxLineGap=px_from_m(crop, 0.5),
    )
    return np.zeros((0, 4), dtype=np.float64) if lines is None else lines.reshape(-1, 4)


def _flank_statistics(
    crop: ImageCrop, interior: np.ndarray, shadowed: np.ndarray, line: np.ndarray, cfg: Any
) -> dict[str, float] | None:
    """Brightness and shadow statistics of the two bands flanking ``line``.

    A gable ridge separates two *lit* planes with different sun aspects, so they differ in
    brightness. A parapet edge or a panel row usually has similar surfaces on both sides, and a
    shadow boundary has a strong brightness step whose dark flank is classified shadow — so the
    contrast alone cannot separate a ridge from a cast-shadow edge, and the shadow overlap of
    the darker flank is measured as well. Each flank is a *band* from a third of to twice the
    plane-sampling offset, not one line of pixels, so dappled shadow (trees, masts) cannot slip
    between two sample rows. ``None`` when either side lacks enough pixels to sample; the
    flanks are sampled on the roof interior *including* its shadowed part, because excluding
    shadow here would blind the very test that detects a shadow boundary.
    """
    grey = luminance(crop).astype(np.float32)
    x0, y0, x1, y1 = line
    length = math.hypot(x1 - x0, y1 - y0)
    if length <= 0:
        return None
    nx, ny = -(y1 - y0) / length, (x1 - x0) / length
    offset_m = float(param(cfg, "ridge_plane_offset_m"))
    offsets = [
        px_from_m(crop, metres)
        for metres in np.arange(offset_m / 3.0, 2.0 * offset_m + 1e-9, offset_m / 3.0)
    ]
    steps = np.linspace(0.15, 0.85, 25)  # skip the ends, where a ridge meets a hip or a wall
    xs = x0 + steps * (x1 - x0)
    ys = y0 + steps * (y1 - y0)

    def sample(sign: int) -> tuple[np.ndarray, np.ndarray]:
        values: list[np.ndarray] = []
        shadow_hits: list[np.ndarray] = []
        for offset in offsets:
            cols = np.rint(xs + sign * offset * nx).astype(int)
            rows = np.rint(ys + sign * offset * ny).astype(int)
            inside = (
                (cols >= 0)
                & (rows >= 0)
                & (cols < grey.shape[1])
                & (rows < grey.shape[0])
            )
            cols, rows = cols[inside], rows[inside]
            keep = interior[rows, cols]
            values.append(grey[rows[keep], cols[keep]])
            shadow_hits.append(shadowed[rows[keep], cols[keep]])
        return np.concatenate(values), np.concatenate(shadow_hits)

    side_a, shadow_a = sample(+1)
    side_b, shadow_b = sample(-1)
    if side_a.size < 5 or side_b.size < 5:
        return None
    mean_a, mean_b = float(side_a.mean()), float(side_b.mean())
    if mean_a + mean_b <= 0:
        return None
    darker_shadow = shadow_a if mean_a <= mean_b else shadow_b
    return {
        "contrast": abs(mean_a - mean_b) / (mean_a + mean_b),
        "mean_bright_side": max(mean_a, mean_b),
        "mean_dark_side": min(mean_a, mean_b),
        "darker_flank_shadow_overlap": float(darker_shadow.mean()),
    }


def _medial_axis_ratio(crop: ImageCrop, line: np.ndarray) -> tuple[float, float]:
    """How centrally the segment crests its perpendicular roof chord: ``(ratio, median_m)``.

    A ridge is a crest: every interior point of a pitched roof section drains to one of the two
    eaves the roof falls to, and the crest sits where the two distances meet — on the local
    medial axis of its roof section. A facade edge, parapet, terrace/deck step or ortho-lean
    artefact is the *boundary* of a sub-roof and runs off-axis. The candidate segment is first
    *extended along its own direction* to where the roof ends on each side — a ridge line
    continues medially for the length of its roof section, and evaluating only the detected
    fragment would let a fragment that happens to sit near a transverse edge score as medial.
    For each sample of the extended span the statistic compares its distance-from-outline with
    the largest such distance found along the perpendicular roof chord through it (the local
    medial radius), so the test is scale-free: a symmetric gable scores 1.0 on a narrow wing
    and on a wide hall alike, and a 40/60 plane-width asymmetry scores 0.8. The *median* over
    the span is used because a genuine gable ridge legitimately reaches the outline at its
    gable ends. Interior-ring (courtyard) edges count as outline: they bound roof planes
    exactly as street-side eaves do, and they stop both the extension and the chords.

    Known limitation, documented rather than patched: on a *compact square* section, samples
    near the transverse ends of the crossing score ~1 by construction (the transverse edge
    limits both the sample and its chord), so an off-axis line on a square roof can be scored
    medial by the median. Real terrace/deck/facade steps run along *elongated* sections —
    where the median is decided by the informative middle — and a low quantile that would
    catch the square case was measured to reject genuine asymmetric-mansard ridges, which is
    the worse error under this package's precision-over-recall stance.
    """
    roof = np.asarray(crop.roof_mask, dtype=bool)
    distance_px = cv2.distanceTransform(roof.astype(np.uint8), cv2.DIST_L2, 3)
    x0, y0, x1, y1 = [float(v) for v in line]
    length = math.hypot(x1 - x0, y1 - y0)
    if length <= 0:
        return 0.0, 0.0
    dx, dy = (x1 - x0) / length, (y1 - y0) / length
    nx, ny = -dy, dx
    height, width = roof.shape
    reach = np.arange(1, max(height, width))

    def walk(px: float, py: float, ux: float, uy: float) -> float:
        """Steps from (px, py) along (ux, uy) until the roof ends (or the image does)."""
        cols = np.rint(px + reach * ux).astype(int)
        rows = np.rint(py + reach * uy).astype(int)
        inside = (cols >= 0) & (rows >= 0) & (cols < width) & (rows < height)
        cols, rows = cols[inside], rows[inside]
        on_roof = roof[rows, cols]
        return float(np.argmin(on_roof) if not on_roof.all() else len(on_roof))

    mid_x, mid_y = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
    forward = walk(mid_x, mid_y, dx, dy)
    backward = walk(mid_x, mid_y, -dx, -dy)
    span = forward + backward
    if span <= 0:
        return 0.0, 0.0
    start_x, start_y = mid_x - backward * dx, mid_y - backward * dy

    ratios: list[float] = []
    distances: list[float] = []
    for step in np.linspace(0.0, 1.0, 50):
        col = min(width - 1, max(0, int(round(start_x + step * span * dx))))
        row = min(height - 1, max(0, int(round(start_y + step * span * dy))))
        if not roof[row, col]:
            continue
        here = float(distance_px[row, col])
        largest = here
        for sign in (1.0, -1.0):
            cols = np.rint(col + sign * reach * nx).astype(int)
            rows = np.rint(row + sign * reach * ny).astype(int)
            inside = (cols >= 0) & (rows >= 0) & (cols < width) & (rows < height)
            cols, rows = cols[inside], rows[inside]
            on_roof = roof[rows, cols]
            off = int(np.argmin(on_roof)) if not on_roof.all() else len(on_roof)
            if off:
                largest = max(largest, float(distance_px[rows[:off], cols[:off]].max()))
        if largest > 0:
            ratios.append(here / largest)
            distances.append(here)
    if not ratios:
        return 0.0, 0.0
    return (
        float(np.median(ratios)),
        float(np.median(distances)) * ground_pixel_size_m(crop),
    )


def _axis_misalignment_deg(azimuth: float, roof_geom_wgs84: Any, metric_crs: str) -> float | None:
    """Angular distance from the ridge azimuth to the nearer footprint-rectangle axis.

    The main ridge of a pitched roof runs parallel to a dominant footprint direction — the long
    axis for gables and hips, the short axis for cross-gables — so the candidate is compared
    against both rectangle axes (90 deg apart) and the smaller distance is returned. ``None``
    when the outline has no rotated-rectangle axis to measure.
    """
    projected, _ = to_metric(roof_geom_wgs84, metric_crs)
    try:
        axis, _, _ = min_rotated_rect_axis(projected)
    except ValueError:
        return None

    def fold(a: float, b: float) -> float:
        d = abs(a - b) % 180.0
        return min(d, 180.0 - d)

    return min(fold(azimuth, axis), fold(azimuth, (axis + 90.0) % 180.0))


def observe_ridge_orientation(
    crop: ImageCrop, cfg: Any, roof_geom_wgs84: Any | None = None
) -> ImageObservation:
    """Azimuth of the detected ridge line, or ``None`` when no ridge is detected.

    Every condition must hold together, and any one failing gives ``None``: at least
    ``ridge_min_supporting_lines`` Hough segments agree on an orientation within tolerance,
    those segments carry the majority of the detected line length, the longest of them reaches
    the minimum ground length, and the strongest candidate passes the quality gates —
    brightness contrast between its two flanks, a darker flank that is *not* classified shadow,
    interior clearance from the outline, and (when the authoritative outline is supplied)
    alignment with a footprint-rectangle axis. Line agreement alone is not enough — a flat roof
    with a parapet and a plant row produces co-oriented lines, and a cast-shadow edge produces
    a brightness step. Every gate value and its margin is published in ``quality["gates"]``.
    """
    _, quality = judgeable_mask(crop, cfg)
    shadowed = shadow_mask(crop, cfg)
    # Eroded so Hough cannot lock onto the footprint outline itself, with the no-data area
    # removed so the straight edge of a missing tile is not detected as a ridge, and with the
    # shadowed area removed so the edge of a cast shadow is not searched at all.
    eroded_with_data = (
        cv2.erode(
            np.asarray(crop.roof_mask, dtype=np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            iterations=px_from_m(crop, param(cfg, "ridge_interior_erode_m")),
        )
        > 0
    ) & ~no_data_mask(crop)
    interior = eroded_with_data & ~shadowed
    tolerance = param(cfg, "ridge_orientation_tolerance_deg")
    min_lines = int(param(cfg, "ridge_min_supporting_lines"))
    share_min = param(cfg, "ridge_dominant_length_share_min")
    contrast_min = param(cfg, "ridge_plane_contrast_min")
    flank_shadow_max = float(threshold(cfg, "image", "ridge", "shadow_flank_overlap_max"))
    medial_min = float(threshold(cfg, "image", "ridge", "medial_axis_ratio_min"))
    axis_tolerance = float(threshold(cfg, "image", "ridge", "axis_alignment_tolerance_deg"))
    quality["thresholds"] = {
        "ridge_min_length_m": param(cfg, "ridge_min_length_m"),
        "orientation_tolerance_deg": tolerance,
        "min_supporting_lines": min_lines,
        "dominant_length_share_min": share_min,
        "plane_contrast_min": contrast_min,
        "shadow_flank_overlap_max": flank_shadow_max,
        "medial_axis_ratio_min": medial_min,
        "axis_alignment_tolerance_deg": axis_tolerance,
    }

    def absent(reason: str) -> ImageObservation:
        return ImageObservation(
            name="ridge_orientation_deg",
            value=None,
            method=RIDGE_METHOD,
            rationale=(
                f"no ridge line detected: {reason}. No orientation is reported; absence of a "
                f"detected ridge is not evidence that the roof has none"
            ),
            quality=quality,
            limitations=RIDGE_LIMITATIONS,
        )

    if not interior.any():
        return absent("the roof interior vanishes under the erosion used to keep Hough off the "
                      "outline, so there is no interior to search")

    lines = _hough_lines(crop, interior, cfg)
    quality["n_lines"] = int(len(lines))
    if len(lines) < min_lines:
        return absent(f"{len(lines)} line segments found, fewer than the {min_lines} required")

    lengths = np.hypot(lines[:, 2] - lines[:, 0], lines[:, 3] - lines[:, 1])
    angles = np.degrees(np.arctan2(lines[:, 3] - lines[:, 1], lines[:, 2] - lines[:, 0])) % 180.0
    # Doubled-angle vector mean: orientations live on a 180 deg circle, so a naive mean would put
    # the average of 179 deg and 1 deg at 90 deg instead of 0.
    doubled = np.radians(2.0 * angles)
    vx = float((lengths * np.cos(doubled)).sum())
    vy = float((lengths * np.sin(doubled)).sum())
    dominant = (math.degrees(math.atan2(vy, vx)) / 2.0) % 180.0
    delta = np.minimum(np.abs(angles - dominant), 180.0 - np.abs(angles - dominant))
    supporting = delta <= tolerance
    share = float(lengths[supporting].sum() / lengths.sum())
    quality["dominant_pixel_angle_deg"] = round(dominant, 2)
    quality["n_supporting_lines"] = int(supporting.sum())
    quality["dominant_length_share"] = round(share, 4)

    if int(supporting.sum()) < min_lines:
        return absent(
            f"only {int(supporting.sum())} of {len(lines)} segments agree within "
            f"{tolerance:.0f} deg of the dominant orientation"
        )
    if share < share_min:
        return absent(
            f"the dominant orientation carries only {share:.0%} of the detected line length, "
            f"below {share_min:.0%}: the lines disagree"
        )

    order = np.argsort(-lengths[supporting])
    best = lines[supporting][order[0]]
    # Ground metres via the same conversion every metre threshold in the package uses.
    best_length_m = float(lengths[supporting][order[0]]) * ground_pixel_size_m(crop)
    # Approximate: measured on the image pixel grid, not in EPSG:31256. Suffixed and labelled the
    # way imaging.roof_mask_area_m2_approx is, so it is never read as a published metric value.
    quality["longest_supporting_line_m_approx"] = round(best_length_m, 2)
    quality["longest_supporting_line_measurement_frame"] = "local_ground_scale_cos_latitude"
    quality["longest_supporting_line_source_crs"] = "EPSG:3857"
    quality["longest_supporting_line_note"] = (
        "approximate ridge-segment length from the image grid, used only to gate the "
        "ridge_min_length_m threshold in the same frame; no published attribute value is derived "
        "from it"
    )
    if best_length_m < param(cfg, "ridge_min_length_m"):
        return absent(
            f"the longest agreeing segment is {best_length_m:.1f} m, below the "
            f"{param(cfg, 'ridge_min_length_m'):.1f} m minimum"
        )

    # --- quality gates on the strongest candidate. Every gate value is published with its
    # threshold and a signed margin (positive = passed, in units of the threshold), so the
    # decision is machine-readable and reproducible from the record itself.
    # Flanks are sampled on the eroded roof INCLUDING its shadowed part: excluding shadow here
    # would blind the very test that detects a shadow boundary.
    flanks = _flank_statistics(crop, eroded_with_data, shadowed, best, cfg)
    contrast = None if flanks is None else flanks["contrast"]
    quality["plane_brightness_contrast"] = None if contrast is None else round(contrast, 4)

    lon0, lat0 = pixel_to_lonlat(crop, float(best[0]), float(best[1]))
    lon1, lat1 = pixel_to_lonlat(crop, float(best[2]), float(best[3]))
    metric_crs = str(threshold(cfg, "crs", "metric"))
    azimuth = azimuth_deg((lon0, lat0), (lon1, lat1), metric_crs)
    quality["crs"] = metric_crs

    medial_ratio, clearance_m = _medial_axis_ratio(crop, best)
    misalignment = (
        None
        if roof_geom_wgs84 is None
        else _axis_misalignment_deg(azimuth, roof_geom_wgs84, metric_crs)
    )
    gates: dict[str, Any] = {
        "plane_contrast": {
            "value": None if contrast is None else round(contrast, 4),
            "min": contrast_min,
            "passed": contrast is not None and contrast >= contrast_min,
        },
        "shadow_flank_overlap": {
            "value": (
                None if flanks is None else round(flanks["darker_flank_shadow_overlap"], 4)
            ),
            "max": flank_shadow_max,
            "passed": (
                flanks is not None
                and flanks["darker_flank_shadow_overlap"] <= flank_shadow_max
            ),
        },
        "medial_axis_ratio": {
            "value": round(medial_ratio, 4),
            "min": medial_min,
            "passed": medial_ratio >= medial_min,
            # Informational companion in metres; the decision is the scale-free ratio.
            "median_distance_from_outline_m": round(clearance_m, 2),
        },
        "axis_alignment_deg": {
            "value": None if misalignment is None else round(misalignment, 2),
            "max": axis_tolerance,
            # Not applicable without an outline: the gate neither passes nor fails, and the
            # published value says why.
            "passed": True if misalignment is None else misalignment <= axis_tolerance,
            "note": "not evaluated: no authoritative outline supplied" if misalignment is None
            else None,
        },
    }
    margins = [
        (contrast - contrast_min) / contrast_min if contrast is not None else None,
        (
            (flank_shadow_max - flanks["darker_flank_shadow_overlap"]) / flank_shadow_max
            if flanks is not None
            else None
        ),
        (medial_ratio - medial_min) / medial_min,
        (
            (axis_tolerance - misalignment) / axis_tolerance
            if misalignment is not None
            else None
        ),
    ]
    evaluated = [m for m in margins if m is not None]
    quality["gates"] = gates
    quality["gate_margin"] = round(min(evaluated), 4) if evaluated else None

    if flanks is None:
        return absent("the two sides of the strongest line could not both be sampled inside the "
                      "roof interior, so no plane contrast test was possible")
    if contrast < contrast_min:
        return absent(
            f"brightness across the strongest line differs by only {contrast:.1%}, below "
            f"{contrast_min:.1%}: consistent with a parapet, seam or panel row rather than a "
            f"ridge between two differently-facing planes"
        )
    if flanks["darker_flank_shadow_overlap"] > flank_shadow_max:
        return absent(
            f"{flanks['darker_flank_shadow_overlap']:.0%} of the darker flank of the strongest "
            f"line is classified shadow (gate {flank_shadow_max:.0%}): the brightness step is "
            f"an illumination boundary, not two differently-facing lit planes"
        )
    if medial_ratio < medial_min:
        return absent(
            f"the strongest line runs at {medial_ratio:.0%} of the local medial radius from "
            f"the outline (gate {medial_min:.0%}, median {clearance_m:.1f} m in): consistent "
            f"with a facade, parapet, deck or terrace edge bounding a sub-roof rather than a "
            f"ridge cresting one"
        )
    if misalignment is not None and misalignment > axis_tolerance:
        return absent(
            f"the strongest line sits {misalignment:.0f} deg from the nearer footprint-"
            f"rectangle axis, beyond the {axis_tolerance:.0f} deg tolerance: a ridge on a "
            f"pitched roof runs parallel to a dominant footprint direction"
        )

    # Every gate passed: the accepted supporting segment itself is published, endpoints in
    # WGS84, so the overlay can draw the OBSERVED evidence rather than synthesising a line
    # from the azimuth. Recorded only on acceptance — a withheld ridge leaves nothing to draw.
    quality["accepted_segment"] = {
        "endpoints_lonlat": [
            [round(lon0, 8), round(lat0, 8)],
            [round(lon1, 8), round(lat1, 8)],
        ],
        "crs": "EPSG:4326",
        "note": (
            "endpoints of the accepted ridge-support segment (the longest supporting Hough "
            "segment, the one every quality gate was evaluated on); the published value is "
            "the azimuth derived from it"
        ),
    }

    return ImageObservation(
        name="ridge_orientation_deg",
        value=round(azimuth, 2),
        method=RIDGE_METHOD,
        rationale=(
            f"{int(supporting.sum())} Hough segments agree within {tolerance:.0f} deg and carry "
            f"{share:.0%} of the detected line length; the strongest is {best_length_m:.1f} m "
            f"long with a {contrast:.1%} brightness difference across it, an unshadowed darker "
            f"flank, and runs at {medial_ratio:.0%} of the local medial radius"
            + (
                f", {misalignment:.0f} deg from the nearer footprint axis"
                if misalignment is not None
                else ""
            )
            + f" - consistent with a ridge cresting two differently-facing planes. Grid "
            f"bearing in {metric_crs}"
        ),
        quality=quality,
        limitations=RIDGE_LIMITATIONS,
    )


def observe(
    crop: ImageCrop, roof_geom_wgs84: Any, cfg: Any
) -> tuple[ImageObservation, ImageObservation]:
    """Both orientation observations, in the order (ridge, footprint axis).

    Returned as a tuple rather than a single record so that no caller can accidentally publish
    one as the other. The outline is passed to the ridge detector so its axis-alignment gate
    can run; the ridge value itself still comes from pixels alone.
    """
    return (
        observe_ridge_orientation(crop, cfg, roof_geom_wgs84),
        observe_footprint_axis(roof_geom_wgs84, cfg),
    )
