"""Visible roof surface class from RGB colour and texture statistics.

The field is a **visible surface class**, not a construction material. Three-band aerial RGB at
15 cm cannot distinguish a bitumen membrane from an EPDM one, or clay tile from a concrete tile
moulded to look like it; it can only report what the surface looks like from above. Every
rationale this module emits says so, because "material: metal" would be a claim the data cannot
support, and design 5 caps the attribute at 0.60 for exactly that reason.

Classification is per pixel and then aggregated, rather than one decision on whole-roof means.
An articulated roof with a tiled main body and a metal dormer has means that belong to neither
class; pixel shares make ``mixed`` an available answer instead of an averaged wrong one.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from ..imaging import luminance, px_from_m, threshold
from ..types import ImageCrop, ImageObservation
from . import judgeable_mask, param

CLASSES = ("tiled", "bitumen_gravel", "metal", "vegetated", "mixed", "unknown")

METHOD = "rgb_colour_and_texture_statistics"

LIMITATIONS = (
    "visible surface class only; RGB at 15 cm cannot establish construction material",
    "15 cm GSD with JPEG compression; small dormers and plant rooms may dominate a small roof",
    "pixels below the configured shadow-luminance threshold are excluded; on this dataset that "
    "excluded none, so a self-shadowed plane was classified alongside its lit ones",
    "dormant March/April 2024 vegetation reads as reddish-brown substrate, so an extensive "
    "green roof can classify as bitumen_gravel rather than vegetated (design 5.2)",
)


def _texture(crop: ImageCrop, cfg: Any) -> np.ndarray:
    """Local roughness: mean absolute Laplacian over a ground-metre window.

    The Laplacian responds to the tile courses, gravel speckle and seam lines that separate the
    classes; averaging it over ~0.5 m turns a per-pixel edge response into a surface property.
    """
    grey = luminance(crop).astype(np.float32)
    detail = np.abs(cv2.Laplacian(grey, cv2.CV_32F, ksize=3))
    radius = px_from_m(crop, param(cfg, "texture_window_m"))
    return cv2.boxFilter(detail, -1, (2 * radius + 1, 2 * radius + 1))


def observe_surface_class(crop: ImageCrop, cfg: Any) -> ImageObservation:
    """Classify the visible roof surface inside the authoritative mask.

    Returns an observation whose value is one of :data:`CLASSES`, or ``None`` when the crop
    contains no judgeable roof pixel at all. ``unknown`` is the value when pixels were judged but
    no class owns enough of them — a deliberate outcome, not a fallback.
    """
    judgeable, quality = judgeable_mask(crop, cfg)
    quality["classes_considered"] = list(CLASSES)
    n = int(judgeable.sum())
    if n == 0:
        return ImageObservation(
            name="roof_surface_class",
            value=None,
            method=METHOD,
            rationale=(
                "no judgeable roof pixels: the crop has no roof interior bright enough to "
                "classify, so no visible surface class is reported"
            ),
            quality=quality,
            limitations=LIMITATIONS,
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
    metal = rest & (saturation <= sat_low) & (texture < smooth) & (value >= metal_value)
    rest = rest & ~metal
    bitumen = rest & (saturation <= sat_low) & (texture >= smooth)

    shares = {
        "tiled": float(tiled.sum()) / n,
        "bitumen_gravel": float(bitumen.sum()) / n,
        "metal": float(metal.sum()) / n,
        "vegetated": float(vegetated.sum()) / n,
    }
    ranked = sorted(shares.items(), key=lambda kv: kv[1], reverse=True)
    dominant_min = param(cfg, "dominant_share_min")
    pair_min = param(cfg, "mixed_pair_share_min")

    quality.update(
        {
            "pixel_shares": {k: round(v, 4) for k, v in shares.items()},
            "unclassified_share": round(1.0 - sum(shares.values()), 4),
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
            },
        }
    )

    top_name, top_share = ranked[0]
    second_name, second_share = ranked[1]
    if top_share >= dominant_min:
        chosen, reason = top_name, (
            f"{top_share:.0%} of judgeable roof pixels match the {top_name} signature"
        )
    elif top_share + second_share >= pair_min:
        chosen, reason = "mixed", (
            f"no single signature dominates: {top_name} {top_share:.0%} and "
            f"{second_name} {second_share:.0%} share the roof"
        )
    else:
        chosen, reason = "unknown", (
            f"no signature reaches {dominant_min:.0%} of judgeable pixels "
            f"(strongest: {top_name} {top_share:.0%})"
        )

    return ImageObservation(
        name="roof_surface_class",
        value=chosen,
        method=METHOD,
        rationale=(
            f"visible surface class from RGB colour and texture inside the authoritative "
            f"outline: {reason}. This describes what the surface looks like in the 2024 "
            f"orthophoto and is never a claim about construction material."
        ),
        quality=quality,
        limitations=LIMITATIONS,
    )
