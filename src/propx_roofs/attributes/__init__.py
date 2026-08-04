"""Image-derived observations of one roof.

Every public function in this package returns an
:class:`~propx_roofs.types.ImageObservation` and **never** an
:class:`~propx_roofs.types.Attribute`: provenance, caps and confidence belong to
``confidence.py`` / ``provenance.py``. Keeping the split means the image stage cannot quietly
publish a value, and the confidence stage cannot quietly re-measure one.

``value is None`` means *could not determine*. Design 6.1 makes that asymmetric on purpose:
absence of evidence is weak evidence of absence, and the city's own 7.5 cm pipeline recovers only
~70% of existing installations (design 1.6), so nothing detected under a shadowed roof is
``None``, not ``False``. :func:`resolve_asymmetric_detection` is the one implementation of that
rule, shared by ``solar.py`` and ``vegetation.py`` so the two cannot drift apart.

``ATTRIBUTE_PARAMS`` holds algorithm-shape parameters in ground metres or as ratios of the
observed pixel population. The published decision thresholds — ``image.shadow_*``,
``image.solar_panels.*``, ``image.green_roof.*`` — are read directly from
``configs/pipeline.yaml`` and are never duplicated here. :func:`param` prefers an ``image.tuning``
block in that file if one is ever added.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..imaging import luminance, no_data_mask, shadow_stats, threshold
from ..types import ImageCrop

ATTRIBUTE_PARAMS: dict[str, float] = {
    # --- surface class (surface.py) -------------------------------------------------------
    # Texture is the mean absolute Laplacian smoothed over this radius. 0.5 m spans ~5 pixels
    # at z20: wide enough to see a tile course or gravel speckle, narrow enough not to blur a
    # whole plane into its neighbours.
    "texture_window_m": 0.5,
    # Below this the surface is smooth at 15 cm (sheet metal, membrane); above it is speckled
    # (gravel, tiles). Calibrated against the imagery rather than guessed: JPEG grain alone
    # produces 8-12 on this statistic, so a lower threshold would classify nothing as smooth.
    # The shaded sheet-metal wings of vie-swv-007 measure 12-14; gravel, substrate and tile
    # measure 23-38 across the ten selected buildings.
    "texture_smooth_below": 15.0,
    "tiled_red_ratio_min": 0.08,  # (R-B)/(R+B): fired clay reads warm even when weathered
    # Of 255. Measured on the visibly clay-tiled Gruenderzeit roof of vie-swv-007: mean
    # saturation 32, because the March flight is hazy at low sun elevation. 40 (a reasonable
    # value for summer imagery) rejects real clay tile here; 30 admits it without opening the
    # class to neutral grey, which the red-ratio gate excludes anyway.
    "tiled_saturation_min": 30.0,
    "low_saturation_max": 55.0,  # membrane, gravel and metal are all near-neutral
    "metal_value_min": 80.0,  # of 255; a dark smooth surface is more likely membrane
    # A class must own most of the judgeable pixels to be reported; two classes sharing the roof
    # is "mixed", which is a real answer on an articulated roof and better than picking one.
    "dominant_share_min": 0.55,
    "mixed_pair_share_min": 0.70,
    # --- ridge detection (orientation.py) -------------------------------------------------
    "ridge_interior_erode_m": 1.5,  # keep Hough off the footprint outline itself
    "ridge_min_length_m": 4.0,
    "ridge_orientation_tolerance_deg": 12.0,
    "ridge_min_supporting_lines": 2.0,
    "ridge_dominant_length_share_min": 0.5,
    # A gable ridge separates two planes facing different directions, so the two sides differ in
    # brightness. A parapet or a panel row generally does not. Sampled this far off the line.
    "ridge_plane_offset_m": 1.5,
    # |mean_a - mean_b| / (mean_a + mean_b) across the line. Under the low March sun two planes of
    # a pitched roof differ strongly in incident light, so 15% is a modest demand; below it the
    # line is more likely a panel-array edge, a substrate boundary or a seam. Measured over the
    # ten selected buildings: lines on flat roofs scored 0.09-0.13, lines on the roofs the
    # authoritative source calls pitched scored 0.23-0.29.
    "ridge_plane_contrast_min": 0.15,
    # --- solar arrays (solar.py) ----------------------------------------------------------
    "solar_open_m": 0.3,  # remove single-pixel dark speckle (JPEG noise, gutters)
    "solar_close_m": 0.5,  # bridge the frame gaps inside one module row
    "solar_rectangularity_min": 0.55,  # cluster area / minimum rotated rectangle area
    "solar_orientation_tolerance_deg": 15.0,
    "solar_periodicity_min": 0.15,  # power share of the dominant row spacing
    # A module array is a grid of framed panels with gaps between rows, so its interior carries
    # structure at the half-metre scale. A uniformly dark roof plane, a membrane or a soft cast
    # shadow does not. Without this gate the detector fires on the shaded north planes of pitched
    # roofs, which is the dominant false positive in this study area. Measured over the ten
    # selected buildings (2024 orthophoto, z20): dark clusters on shaded planes and parapet
    # shadows score 8-22 on this statistic, while the module arrays on OBJECTID 351705 and 351704
    # score 27-38. 25 sits in that gap, on the precision side.
    "solar_internal_texture_min": 25.0,
}


def param(cfg: Any, name: str) -> float:
    """Algorithm parameter: ``image.tuning.<name>`` from config, else the documented default."""
    if name not in ATTRIBUTE_PARAMS:
        raise KeyError(f"unknown attribute parameter {name!r}")
    return float(threshold(cfg, "image", "tuning", name, default=ATTRIBUTE_PARAMS[name]))


def judgeable_mask(crop: ImageCrop, cfg: Any) -> tuple[np.ndarray, dict[str, Any]]:
    """Roof pixels bright enough to judge colour and texture on, with the quality numbers.

    Shadowed pixels are excluded because hue and saturation are meaningless there — a dark blue
    shadow on a tiled roof is not a blue roof. Pixels with no cached imagery are excluded too.
    **Detectors looking for dark features must not use this**: ``solar.py`` deliberately keeps the
    whole roof mask minus the no-data area, since a panel is itself dark and excluding dark pixels
    would exclude the target.
    """
    stats = shadow_stats(crop, cfg)
    limit = float(threshold(cfg, "image", "shadow_luminance_threshold"))
    roof = np.asarray(crop.roof_mask, dtype=bool)
    judgeable = roof & ~no_data_mask(crop) & (luminance(crop) >= limit)
    # Denominator is the whole authoritative outline, so judgeable_fraction answers "how much of
    # this roof could we actually look at", which is what a confidence penalty needs.
    n_roof = int(roof.sum())
    stats = dict(stats)
    stats["judgeable_pixel_count"] = int(judgeable.sum())
    stats["judgeable_fraction"] = (
        round(float(judgeable.sum() / n_roof), 4) if n_roof else None
    )
    return judgeable, stats


def resolve_asymmetric_detection(
    detected: bool, shadow_fraction: float | None, abstain_above: float
) -> tuple[bool | None, str]:
    """Apply design 6.1 to a binary image detection. Returns ``(value, rationale fragment)``.

    * strong positive evidence -> ``True`` (evidence present is evidence, shadow or not);
    * nothing detected and image quality good -> ``False``, naming absence of evidence;
    * nothing detected and the roof substantially shadowed -> ``None``, **never** ``False``.

    Abstention is the cheap path here on purpose (design 6): the only way to return ``False`` is
    to have looked at an adequately lit roof and found nothing.
    """
    if detected:
        return True, "positive image evidence detected above the configured thresholds"
    if shadow_fraction is None:
        return None, (
            "abstained: no judgeable roof pixels in the crop, so absence could not be assessed"
        )
    if shadow_fraction > abstain_above:
        return None, (
            f"abstained: nothing detected, but {shadow_fraction:.1%} of the roof interior is "
            f"below the shadow luminance threshold (abstain above {abstain_above:.0%}); a "
            f"shadowed roof cannot support a negative claim"
        )
    return False, (
        "nothing detected with adequate image quality over the roof interior; this is "
        "absence of evidence, which is weak evidence of absence"
    )
