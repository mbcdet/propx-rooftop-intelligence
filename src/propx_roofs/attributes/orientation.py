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
    threshold,
)
from ..types import ImageCrop, ImageObservation
from . import judgeable_mask, param

RIDGE_METHOD = "hough_line_agreement_with_plane_brightness_contrast"
AXIS_METHOD = "minimum_rotated_rectangle_long_axis"

RIDGE_LIMITATIONS = (
    "a ridge is only reported when detected; absence is not evidence of a flat roof",
    "a long parapet, a rooftop plant row or a panel row can present as a line, which is why a "
    "brightness contrast between the two sides is required as well as line agreement",
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


def _plane_contrast(
    crop: ImageCrop, interior: np.ndarray, line: np.ndarray, cfg: Any
) -> float | None:
    """Relative brightness difference across ``line``, sampled on both sides.

    A gable ridge separates two planes with different sun aspects, so they differ in brightness.
    A parapet edge or a panel row usually has similar surfaces on both sides. Requiring this
    contrast is what keeps the ridge observation from firing on every straight line on a flat
    roof. ``None`` when neither side has enough interior pixels to sample.
    """
    grey = luminance(crop).astype(np.float32)
    x0, y0, x1, y1 = line
    length = math.hypot(x1 - x0, y1 - y0)
    if length <= 0:
        return None
    nx, ny = -(y1 - y0) / length, (x1 - x0) / length
    offset = px_from_m(crop, param(cfg, "ridge_plane_offset_m"))
    steps = np.linspace(0.15, 0.85, 25)  # skip the ends, where a ridge meets a hip or a wall
    xs = x0 + steps * (x1 - x0)
    ys = y0 + steps * (y1 - y0)

    def sample(sign: int) -> np.ndarray:
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
        return grey[rows[keep], cols[keep]]

    side_a, side_b = sample(+1), sample(-1)
    if side_a.size < 5 or side_b.size < 5:
        return None
    mean_a, mean_b = float(side_a.mean()), float(side_b.mean())
    if mean_a + mean_b <= 0:
        return None
    return abs(mean_a - mean_b) / (mean_a + mean_b)


def observe_ridge_orientation(crop: ImageCrop, cfg: Any) -> ImageObservation:
    """Azimuth of the detected ridge line, or ``None`` when no ridge is detected.

    Four conditions must hold together, and any one failing gives ``None``: at least
    ``ridge_min_supporting_lines`` Hough segments agree on an orientation within tolerance, those
    segments carry the majority of the detected line length, the longest of them reaches the
    minimum ground length, and the brightness on the two sides of it differs. Line agreement
    alone is not enough — a flat roof with a parapet and a plant row produces co-oriented lines.
    """
    _, quality = judgeable_mask(crop, cfg)
    # Eroded so Hough cannot lock onto the footprint outline itself, and with the no-data area
    # removed so the straight edge of a missing tile is not detected as a ridge.
    interior = (
        cv2.erode(
            np.asarray(crop.roof_mask, dtype=np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            iterations=px_from_m(crop, param(cfg, "ridge_interior_erode_m")),
        )
        > 0
    ) & ~no_data_mask(crop)
    tolerance = param(cfg, "ridge_orientation_tolerance_deg")
    min_lines = int(param(cfg, "ridge_min_supporting_lines"))
    share_min = param(cfg, "ridge_dominant_length_share_min")
    contrast_min = param(cfg, "ridge_plane_contrast_min")
    quality["thresholds"] = {
        "ridge_min_length_m": param(cfg, "ridge_min_length_m"),
        "orientation_tolerance_deg": tolerance,
        "min_supporting_lines": min_lines,
        "dominant_length_share_min": share_min,
        "plane_contrast_min": contrast_min,
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

    contrast = _plane_contrast(crop, interior, best, cfg)
    quality["plane_brightness_contrast"] = None if contrast is None else round(contrast, 4)
    if contrast is None:
        return absent("the two sides of the strongest line could not both be sampled inside the "
                      "roof interior, so no plane contrast test was possible")
    if contrast < contrast_min:
        return absent(
            f"brightness across the strongest line differs by only {contrast:.1%}, below "
            f"{contrast_min:.1%}: consistent with a parapet, seam or panel row rather than a "
            f"ridge between two differently-facing planes"
        )

    lon0, lat0 = pixel_to_lonlat(crop, float(best[0]), float(best[1]))
    lon1, lat1 = pixel_to_lonlat(crop, float(best[2]), float(best[3]))
    metric_crs = str(threshold(cfg, "crs", "metric"))
    azimuth = azimuth_deg((lon0, lat0), (lon1, lat1), metric_crs)
    quality["crs"] = metric_crs
    return ImageObservation(
        name="ridge_orientation_deg",
        value=round(azimuth, 2),
        method=RIDGE_METHOD,
        rationale=(
            f"{int(supporting.sum())} Hough segments agree within {tolerance:.0f} deg and carry "
            f"{share:.0%} of the detected line length; the strongest is {best_length_m:.1f} m "
            f"long with a {contrast:.1%} brightness difference across it, consistent with a "
            f"ridge between two differently-facing planes. Grid bearing in {metric_crs}"
        ),
        quality=quality,
        limitations=RIDGE_LIMITATIONS,
    )


def observe(
    crop: ImageCrop, roof_geom_wgs84: Any, cfg: Any
) -> tuple[ImageObservation, ImageObservation]:
    """Both orientation observations, in the order (ridge, footprint axis).

    Returned as a tuple rather than a single record so that no caller can accidentally publish
    one as the other.
    """
    return observe_ridge_orientation(crop, cfg), observe_footprint_axis(roof_geom_wgs84, cfg)
