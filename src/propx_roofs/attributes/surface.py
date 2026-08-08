"""Visible surface *appearance* from RGB colour and texture statistics.

The published attribute is ``visual_surface_appearance`` — deliberately not a surface *class*
and never a construction material. Three-band aerial RGB at 15 cm cannot distinguish a bitumen
membrane from an EPDM one, or clay tile from a concrete tile moulded to look like it; it can
only report what the surface looks like from above, so every class name here is an appearance
term ending in ``_like`` and every rationale says so. Design 5 caps the attribute at 0.60 for
exactly that reason.

Classification is per pixel and then aggregated, rather than one decision on whole-roof means.
An articulated roof with a tiled main body and a metal dormer has means that belong to neither
class; pixel shares make ``mixed`` an available answer instead of an averaged wrong one.

Only judgeable pixels are classified — unshadowed by the adaptive detector in
:mod:`propx_roofs.imaging` and covered by cached imagery — and the observation *abstains*
(value ``None``) rather than naming an appearance when too little of the roof is judgeable or
when the leading signature's dominance over the runner-up is weaker than its own sensitivity.
The dominance share, the margin over the runner-up and the unclassified fraction are always
published in ``quality`` so a reader can see how solid a verdict is.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from ..imaging import luminance, px_from_m, threshold
from ..types import ImageCrop, ImageObservation
from . import judgeable_mask, param

CLASSES = (
    "tiled_like",              # warm, saturated, coursed: fired-clay appearance
    "gravel_or_bitumen_like",  # near-neutral and rough: gravel, bare bitumen, substrate
    "bright_smooth_like",      # near-neutral, smooth and bright: membrane or sheet metal
    "vegetated_like",          # green by the ExG index
    "mixed",                   # two signatures genuinely share the roof
)

METHOD = "rgb_colour_and_texture_appearance_statistics"

LIMITATIONS = (
    "visible surface appearance only; RGB at 15 cm cannot establish construction material, "
    "which is why every class name is an appearance term",
    "15 cm GSD with JPEG compression; small dormers and plant rooms may dominate a small roof",
    "pixels below the adaptive shadow threshold are excluded, and the observation abstains "
    "when the judgeable share of the roof is too small to support any appearance claim",
    "dormant March/April 2024 vegetation reads as reddish-brown substrate, so an extensive "
    "green roof can classify as gravel_or_bitumen_like rather than vegetated_like (design 5.2)",
)


def _texture(crop: ImageCrop, cfg: Any) -> np.ndarray:
    """Local roughness: mean absolute Laplacian over a ground-metre window.

    The Laplacian responds to the tile courses, gravel speckle and seam lines that separate the
    appearance signatures; averaging it over ~0.5 m turns a per-pixel edge response into a
    surface property.
    """
    grey = luminance(crop).astype(np.float32)
    detail = np.abs(cv2.Laplacian(grey, cv2.CV_32F, ksize=3))
    radius = px_from_m(crop, param(cfg, "texture_window_m"))
    return cv2.boxFilter(detail, -1, (2 * radius + 1, 2 * radius + 1))


def observe_surface_appearance(crop: ImageCrop, cfg: Any) -> ImageObservation:
    """Classify the visible surface appearance inside the authoritative mask.

    Returns an observation whose value is one of :data:`CLASSES` or ``None``. ``None`` is an
    abstention with the reason in the rationale: no judgeable pixels at all, too little of the
    roof judgeable, no signature reaching dominance, or a dominance margin weaker than the
    classifier's own sensitivity. Every deciding number is in ``quality``.
    """
    judgeable, quality = judgeable_mask(crop, cfg)
    quality["classes_considered"] = list(CLASSES)
    min_judgeable = float(threshold(cfg, "image", "surface", "min_judgeable_fraction"))
    min_margin = float(threshold(cfg, "image", "surface", "min_dominance_margin"))
    n = int(judgeable.sum())

    def abstain(reason: str) -> ImageObservation:
        return ImageObservation(
            name="visual_surface_appearance",
            value=None,
            method=METHOD,
            rationale=(
                f"no visible surface appearance is reported: {reason}. Abstention, not a "
                f"finding about the roof"
            ),
            quality=quality,
            limitations=LIMITATIONS,
        )

    if n == 0:
        return abstain(
            "the crop has no judgeable roof interior (unshadowed, with imagery) to classify"
        )

    rgb = crop.rgb.astype(np.float32)
    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    total = np.clip(red + green + blue, 1.0, None)
    exg = (2.0 * green - red - blue) / total
    red_ratio = (red - blue) / np.clip(red + blue, 1.0, None)
    hsv = cv2.cvtColor(crop.rgb, cv2.COLOR_RGB2HSV)
    saturation = hsv[..., 1].astype(np.float32)
    value = hsv[..., 2].astype(np.float32)
    texture = _texture(crop, cfg)

    exg_min = float(threshold(cfg, "image", "green_roof", "exg_threshold"))
    red_min = param(cfg, "tiled_red_ratio_min")
    sat_tiled = param(cfg, "tiled_saturation_min")
    sat_low = param(cfg, "low_saturation_max")
    smooth = param(cfg, "texture_smooth_below")
    metal_value = param(cfg, "metal_value_min")

    # Ordered and mutually exclusive: each test only sees pixels the earlier tests rejected, so
    # the shares below sum to at most 1 and "unclassified" is visible rather than absorbed.
    vegetated = judgeable & (exg >= exg_min)
    rest = judgeable & ~vegetated
    tiled = rest & (red_ratio >= red_min) & (saturation >= sat_tiled)
    rest = rest & ~tiled
    bright_smooth = rest & (saturation <= sat_low) & (texture < smooth) & (value >= metal_value)
    rest = rest & ~bright_smooth
    rough_neutral = rest & (saturation <= sat_low) & (texture >= smooth)

    shares = {
        "tiled_like": float(tiled.sum()) / n,
        "gravel_or_bitumen_like": float(rough_neutral.sum()) / n,
        "bright_smooth_like": float(bright_smooth.sum()) / n,
        "vegetated_like": float(vegetated.sum()) / n,
    }
    ranked = sorted(shares.items(), key=lambda kv: kv[1], reverse=True)
    dominant_min = param(cfg, "dominant_share_min")
    pair_min = param(cfg, "mixed_pair_share_min")
    top_name, top_share = ranked[0]
    second_name, second_share = ranked[1]
    margin = top_share - second_share
    judgeable_fraction = quality.get("judgeable_fraction")

    quality.update(
        {
            "pixel_shares": {k: round(v, 4) for k, v in shares.items()},
            "unclassified_share": round(1.0 - sum(shares.values()), 4),
            "dominance_share": round(top_share, 4),
            "margin_over_runner_up": round(margin, 4),
            "runner_up": second_name,
            "mean_saturation": round(float(saturation[judgeable].mean()), 1),
            "mean_value": round(float(value[judgeable].mean()), 1),
            "mean_texture": round(float(texture[judgeable].mean()), 2),
            "mean_red_ratio": round(float(red_ratio[judgeable].mean()), 4),
            "mean_exg": round(float(exg[judgeable].mean()), 4),
            "thresholds": {
                "exg_threshold": exg_min,
                "tiled_red_ratio_min": red_min,
                "tiled_saturation_min": sat_tiled,
                "low_saturation_max": sat_low,
                "texture_smooth_below": smooth,
                "metal_value_min": metal_value,
                "dominant_share_min": dominant_min,
                "mixed_pair_share_min": pair_min,
                "min_judgeable_fraction": min_judgeable,
                "min_dominance_margin": min_margin,
            },
        }
    )

    if judgeable_fraction is not None and judgeable_fraction < min_judgeable:
        return abstain(
            f"only {judgeable_fraction:.0%} of the roof is judgeable (unshadowed, with "
            f"imagery), below the {min_judgeable:.0%} needed to support an appearance claim"
        )

    if top_share >= dominant_min:
        if margin < min_margin:
            return abstain(
                f"the leading signature ({top_name}, {top_share:.0%}) beats the runner-up "
                f"({second_name}, {second_share:.0%}) by only {margin:.0%} of judgeable "
                f"pixels, below the {min_margin:.0%} margin the class boundaries can resolve"
            )
        chosen, reason = top_name, (
            f"{top_share:.0%} of judgeable roof pixels match the {top_name} signature, "
            f"{margin:.0%} ahead of the runner-up ({second_name})"
        )
    elif top_share + second_share >= pair_min:
        chosen, reason = "mixed", (
            f"no single signature dominates: {top_name} {top_share:.0%} and "
            f"{second_name} {second_share:.0%} share the roof"
        )
    else:
        return abstain(
            f"no signature reaches {dominant_min:.0%} of judgeable pixels and no pair reaches "
            f"{pair_min:.0%} (strongest: {top_name} {top_share:.0%}); the surface matches no "
            f"appearance signature well enough to name one"
        )

    return ImageObservation(
        name="visual_surface_appearance",
        value=chosen,
        method=METHOD,
        rationale=(
            f"visible surface appearance from RGB colour and texture inside the authoritative "
            f"outline: {reason}. This describes what the surface looks like in the 2024 "
            f"orthophoto and is never a claim about construction material."
        ),
        quality=quality,
        limitations=LIMITATIONS,
    )


# Backwards-compatible alias for the pre-rename entry point. The contract change is the
# attribute name; keeping the old callable importable makes the rename greppable rather than
# silently breaking downstream callers with an AttributeError at runtime.
observe_surface_class = observe_surface_appearance
