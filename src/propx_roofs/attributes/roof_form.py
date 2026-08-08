"""Symmetric image evidence for flat-versus-pitched roof form.

Before this module the image stage could only say "pitched or nothing": a gated ridge detection
indicated ``pitched`` and everything else abstained, so an authoritative ``pitched`` against a
flat-looking image could never be flagged. This observation closes that asymmetry — but only by
*positive* evidence in both directions:

* ``"pitched"`` on a gated ridge detection (:func:`propx_roofs.attributes.orientation
  .observe_ridge_orientation` with every quality gate passed);
* ``"flat"`` only on positive evidence of flatness measured on judgeable pixels: enough of the
  roof judgeable to support the claim, *and* no brightness partition of the judgeable interior
  strong enough to be two differently-lit planes. The statistic is the best two-mode (Otsu)
  split of the interior brightness, expressed as the same normalised contrast the ridge
  detector demands across a single candidate line — if the *best possible* split of the whole
  interior stays below what one ridge boundary would need, no two differently-facing planes
  are present. Using the same threshold keeps the two directions symmetric by construction.
* ``None`` otherwise: a missed ridge on a shadowed or busy roof is not evidence of flatness.

The observation never touches the authoritative ``DACHFORM`` value and is not itself wired to a
published attribute here; the pipeline's conflict handling decides what to do with it.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from ..imaging import ground_pixel_size_m, luminance, px_from_m, threshold
from ..types import ImageCrop, ImageObservation
from . import judgeable_mask, param
from .orientation import observe_ridge_orientation

METHOD = "gated_ridge_or_positive_flatness_evidence"

LIMITATIONS = (
    "flat is asserted only on positive evidence (sufficient judgeable area and no two-mode "
    "brightness partition of ridge strength); a missed ridge alone never indicates flat",
    "pitched is asserted only on a fully gated ridge detection; a pitched roof whose ridge is "
    "shadowed, short or dormer-broken yields unknown, not flat",
    "brightness is the only slope proxy available in mono RGB: a flat roof half-covered by a "
    "differently-coloured surface can present two brightness modes and yields unknown",
    "measured on the ten-building study sample, no real flat roof passed the flatness bound - "
    "panel fields, terraces and substrate patches all exceed it - so under-assertion of flat "
    "is the expected behaviour on busy urban roofs; that is the accepted conservative "
    "direction, and spatial-coherence refinements were measured on the sample and found not "
    "to separate the classes in a principled way",
    "unknown is an accepted output; the observation abstains rather than guessing",
)


def _otsu_split(values: np.ndarray) -> tuple[float, float, float] | None:
    """Best two-mode split of ``values``: ``(contrast, minor_share, threshold)``.

    ``contrast`` is |mu_hi - mu_lo| / (mu_hi + mu_lo) — the same normalised statistic the ridge
    detector measures across a candidate line — and ``minor_share`` is the pixel share of the
    smaller mode, published so a reader can see whether the split found two planes or one plane
    plus a vent. ``None`` when there are not enough values to split.
    """
    if values.size < 16:
        return None
    histogram = np.bincount(values.astype(np.uint8).ravel(), minlength=256).astype(np.float64)
    total = histogram.sum()
    weights_lo = np.cumsum(histogram)
    weights_hi = total - weights_lo
    levels = np.arange(256, dtype=np.float64)
    sums_lo = np.cumsum(histogram * levels)
    sum_all = sums_lo[-1]
    valid = (weights_lo > 0) & (weights_hi > 0)
    if not valid.any():
        return None  # every pixel identical: nothing to split
    mu_lo = np.where(valid, sums_lo / np.maximum(weights_lo, 1), 0.0)
    mu_hi = np.where(valid, (sum_all - sums_lo) / np.maximum(weights_hi, 1), 0.0)
    between = np.where(valid, weights_lo * weights_hi * (mu_hi - mu_lo) ** 2, -1.0)
    split = int(np.argmax(between))
    lo, hi = float(mu_lo[split]), float(mu_hi[split])
    if lo + hi <= 0:
        return None
    minor = float(min(weights_lo[split], weights_hi[split]) / total)
    return abs(hi - lo) / (hi + lo), minor, float(split)


def observe_roof_form(
    crop: ImageCrop, cfg: Any, roof_geom_wgs84: Any | None = None
) -> ImageObservation:
    """What the imagery says about flat-versus-pitched: ``"flat"``, ``"pitched"`` or ``None``.

    ``quality["margin"]`` is a signed machine-readable margin in units of the deciding
    threshold: for ``pitched`` it is the ridge detector's ``gate_margin``; for ``flat`` it is
    how far the interior's best two-mode contrast sits below the plane-contrast threshold.
    ``quality["features"]`` carries every measured statistic whatever the outcome.
    """
    contrast_max = float(threshold(cfg, "image", "roof_form", "flat_plane_contrast_max"))
    judgeable_min = float(threshold(cfg, "image", "roof_form", "min_judgeable_fraction"))

    ridge = observe_ridge_orientation(crop, cfg, roof_geom_wgs84)
    judgeable, stats = judgeable_mask(crop, cfg)
    judgeable_fraction = stats.get("judgeable_fraction")

    # The same erosion the ridge search uses, so boundary mixed pixels (eaves, parapet edges,
    # ortho lean) cannot manufacture a second brightness mode.
    interior = (
        cv2.erode(
            np.asarray(crop.roof_mask, dtype=np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            iterations=px_from_m(crop, param(cfg, "ridge_interior_erode_m")),
        )
        > 0
    ) & judgeable
    grey = luminance(crop)
    split = _otsu_split(grey[interior])
    contrast = None if split is None else split[0]

    # Large-scale brightness gradient, informational: texture is smoothed away first so gravel
    # speckle does not read as slope. Stated per ground metre so it is zoom-independent.
    blurred = cv2.GaussianBlur(grey.astype(np.float32), (0, 0), px_from_m(crop, 0.5))
    gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1)
    gradient = np.hypot(gx, gy)[interior]
    # The 3x3 Sobel kernel sums to a weight of 8 per unit step, so /8 gives luminance change
    # per pixel; dividing by the ground pixel size states it per metre.
    gradient_per_m = (
        float(gradient.mean()) / 8.0 / ground_pixel_size_m(crop) if gradient.size else None
    )

    quality: dict[str, Any] = dict(stats)
    quality["thresholds"] = {
        "flat_plane_contrast_max": contrast_max,
        "min_judgeable_fraction": judgeable_min,
    }
    quality["features"] = {
        "otsu_plane_contrast": None if contrast is None else round(contrast, 4),
        "otsu_minor_mode_share": None if split is None else round(split[1], 4),
        "otsu_threshold_luminance": None if split is None else split[2],
        "mean_smoothed_gradient": None if gradient_per_m is None else round(gradient_per_m, 2),
        "interior_pixel_count": int(interior.sum()),
    }
    quality["ridge_observation"] = {
        "value": ridge.value,
        "gate_margin": ridge.quality.get("gate_margin"),
        "gates": ridge.quality.get("gates"),
    }

    if ridge.value is not None:
        quality["margin"] = ridge.quality.get("gate_margin")
        return ImageObservation(
            name="image_roof_form",
            value="pitched",
            method=METHOD,
            rationale=(
                f"a gated ridge detection at {ridge.value} deg indicates two differently-facing "
                f"roof planes: {ridge.rationale}"
            ),
            quality=quality,
            limitations=LIMITATIONS,
        )

    if (
        judgeable_fraction is not None
        and judgeable_fraction >= judgeable_min
        and contrast is not None
        and contrast <= contrast_max
    ):
        quality["margin"] = round((contrast_max - contrast) / contrast_max, 4)
        return ImageObservation(
            name="image_roof_form",
            value="flat",
            method=METHOD,
            rationale=(
                f"positive flatness evidence: {judgeable_fraction:.0%} of the roof is judgeable "
                f"(gate {judgeable_min:.0%}) and the best two-mode split of the judgeable "
                f"interior's brightness reaches only {contrast:.1%} normalised contrast, below "
                f"the {contrast_max:.0%} a single ridge boundary would need - no two "
                f"differently-lit planes are present. No gated ridge candidate was accepted"
            ),
            quality=quality,
            limitations=LIMITATIONS,
        )

    quality["margin"] = None
    if judgeable_fraction is None or judgeable_fraction < judgeable_min:
        reason = (
            f"only {0.0 if judgeable_fraction is None else judgeable_fraction:.0%} of the roof "
            f"is judgeable, below the {judgeable_min:.0%} needed to support a flatness claim"
        )
    elif contrast is None:
        reason = "the judgeable interior is too small to measure a brightness partition on"
    else:
        reason = (
            f"the best two-mode split of the judgeable interior reaches {contrast:.1%} "
            f"normalised contrast, at or above the {contrast_max:.0%} threshold: two brightness "
            f"populations are present but no candidate line passed the ridge quality gates, so "
            f"neither flat nor pitched is supported"
        )
    return ImageObservation(
        name="image_roof_form",
        value=None,
        method=METHOD,
        rationale=(
            f"no flat-or-pitched reading is offered from the imagery: {reason}. Absence of a "
            f"detected ridge is not evidence of a flat roof, and a weakly judgeable or "
            f"two-moded roof supports no flatness claim either"
        ),
        quality=quality,
        limitations=LIMITATIONS,
    )
