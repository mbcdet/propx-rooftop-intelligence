"""Visible solar-module detection: dark, low-saturation, regularly spaced rectangular clusters.

**Tuned for precision, not recall.** A false positive puts a fabricated installation into a
published record; a false negative is reported as absence of evidence with a capped confidence
(design 6.1) and the city's own 7.5 cm pipeline only recovers ~70% of existing installations
(design 1.6), so a modest recall is the honest expectation at 15 cm. Every candidate must be
dark *and* near-neutral *and* large *and* rectangular *and* regular — dark glazing, skylight
strips, wet membrane and shaded courtyards each satisfy some of those but rarely all of them.

The whole roof mask is used, not the shadow-filtered one from
:func:`~propx_roofs.attributes.judgeable_mask`: a module is itself dark, so excluding dark pixels
would exclude the target. Shadow enters only through the abstention gate.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from ..imaging import no_data_mask, pixel_area_m2, px_from_m, shadow_stats, threshold
from ..types import ImageCrop, ImageObservation
from . import param, resolve_asymmetric_detection

METHOD = "rgb_dark_low_saturation_rectangular_cluster_regularity"

LIMITATIONS = (
    "15 cm GSD with JPEG compression; the city's own 7.5 cm pipeline reports detecting only "
    "about 70% of existing installations (design 1.6), so recall here is lower still",
    "visible modules only; photovoltaic and solar-thermal collectors are not distinguishable",
    "tuned for precision: dark glazing, skylight strips and wet membrane can mimic modules, so "
    "regularity is required and small isolated arrays are missed",
    "a negative result is absence of evidence, not evidence of absence (design 6.1)",
)


def _ellipse(radius_px: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius_px + 1, 2 * radius_px + 1))


def _internal_texture(crop: ImageCrop, cfg: Any) -> np.ndarray:
    """Local structure map: mean absolute Laplacian over a half-metre window.

    Same statistic as ``surface.py`` uses, applied here as a precision gate. Modules sit in
    frames with gaps between rows, so an array's interior is structured at this scale; a shaded
    roof plane or a soft cast shadow — the dominant false positives in this study area — is
    smooth, and is rejected on that basis rather than on being dark, which it also is.
    """
    grey = cv2.cvtColor(crop.rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    detail = np.abs(cv2.Laplacian(grey, cv2.CV_32F, ksize=3))
    radius = px_from_m(crop, param(cfg, "texture_window_m"))
    return cv2.boxFilter(detail, -1, (2 * radius + 1, 2 * radius + 1))


def _row_periodicity(component: np.ndarray, angle_deg: float) -> float:
    """Strength of a regular row spacing inside one cluster, in [0, 1].

    Module rows on a roof repeat at a fixed pitch, so the cluster's profile perpendicular to the
    rows is periodic. The cluster is rotated so its rectangle sides are axis-aligned, summed along
    rows, and the share of profile power sitting in the single strongest spatial frequency is
    returned. A featureless dark blob scores near zero; a row-and-walkway array scores high. This
    is the "regularly spaced" half of the detector, and it is what a large dark uniform patch
    (a shadow, a pond, a tar repair) fails.
    """
    ys, xs = np.nonzero(component)
    if ys.size == 0:
        return 0.0
    patch = component[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1].astype(np.float32)
    if min(patch.shape) < 8:
        return 0.0
    centre = (patch.shape[1] / 2.0, patch.shape[0] / 2.0)
    rotation = cv2.getRotationMatrix2D(centre, angle_deg, 1.0)
    size = (patch.shape[1], patch.shape[0])
    rotated = cv2.warpAffine(patch, rotation, size, flags=cv2.INTER_NEAREST)
    profile = rotated.sum(axis=1)
    profile = profile - profile.mean()
    if profile.size < 8 or not np.any(profile):
        return 0.0
    power = np.abs(np.fft.rfft(profile)) ** 2
    if power[1:].sum() <= 0:
        return 0.0
    # Bin 1 is the overall taper of the cluster, not a repeat; a real row pitch is bin 2 or above.
    return float(power[2:].max() / power[1:].sum()) if power.size > 2 else 0.0


def observe_solar_panels(crop: ImageCrop, cfg: Any) -> ImageObservation:
    """Detect visible solar modules over the authoritative roof outline.

    ``value`` is ``True`` on strong positive evidence, ``False`` when nothing was found over an
    adequately lit roof, and ``None`` when nothing was found but the roof is too shadowed to
    support a negative claim (design 6.1). The deciding numbers are in ``quality``.
    """
    max_value = float(threshold(cfg, "image", "solar_panels", "max_value"))
    max_saturation = float(threshold(cfg, "image", "solar_panels", "max_saturation"))
    min_cluster_m2 = float(threshold(cfg, "image", "solar_panels", "min_cluster_area_m2"))
    min_coverage = float(threshold(cfg, "image", "solar_panels", "min_coverage_fraction"))
    abstain_above = float(threshold(cfg, "image", "shadow_fraction_abstain"))
    rect_min = param(cfg, "solar_rectangularity_min")
    orientation_tol = param(cfg, "solar_orientation_tolerance_deg")
    periodicity_min = param(cfg, "solar_periodicity_min")
    texture_min = param(cfg, "solar_internal_texture_min")

    quality: dict[str, Any] = dict(shadow_stats(crop, cfg))
    quality["thresholds"] = {
        "max_value": max_value,
        "max_saturation": max_saturation,
        "min_cluster_area_m2": min_cluster_m2,
        "min_coverage_fraction": min_coverage,
        "rectangularity_min": rect_min,
        "orientation_tolerance_deg": orientation_tol,
        "periodicity_min": periodicity_min,
        "internal_texture_min": texture_min,
        "shadow_fraction_abstain": abstain_above,
    }
    # A missing tile is filled with zeros, which is dark and perfectly neutral — the module
    # signature exactly. Excluding the no-data area is what stops a cache gap from being
    # published as an installation.
    roof = np.asarray(crop.roof_mask, dtype=bool) & ~no_data_mask(crop)
    roof_px = int(roof.sum())
    shadow_fraction = quality.get("shadow_fraction")
    quality["roof_pixels_with_imagery"] = roof_px

    if roof_px == 0:
        value, fragment = resolve_asymmetric_detection(False, None, abstain_above)
        return _observation(value, fragment, quality)

    hsv = cv2.cvtColor(crop.rgb, cv2.COLOR_RGB2HSV)
    candidate = roof & (hsv[..., 2] <= max_value) & (hsv[..., 1] <= max_saturation)
    cleaned = cv2.morphologyEx(
        candidate.astype(np.uint8) * 255,
        cv2.MORPH_OPEN,
        _ellipse(px_from_m(crop, param(cfg, "solar_open_m"))),
    )
    cleaned = cv2.morphologyEx(
        cleaned, cv2.MORPH_CLOSE, _ellipse(px_from_m(crop, param(cfg, "solar_close_m")))
    )
    quality["dark_low_saturation_fraction"] = round(float(candidate.sum()) / roof_px, 4)

    pixel_m2 = pixel_area_m2(crop)
    min_cluster_px = int(round(min_cluster_m2 / pixel_m2))
    texture = _internal_texture(crop, cfg)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(cleaned, connectivity=8)
    clusters: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    kept_px = 0
    for label in range(1, count):
        area_px = int(stats[label, cv2.CC_STAT_AREA])
        if area_px < min_cluster_px:
            continue
        selected = labels == label
        component = selected.astype(np.uint8)
        contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        (_, _), (width, height), angle = cv2.minAreaRect(max(contours, key=cv2.contourArea))
        rect_area = width * height
        rectangularity = float(area_px / rect_area) if rect_area > 0 else 0.0
        interior_texture = float(texture[selected].mean())
        record = {
            # Approximate: the pixel-grid frame, not EPSG:31256. Suffixed and labelled the way
            # imaging.roof_mask_area_m2_approx is, so a cluster area is never read as a published
            # metric value. See cluster_area_measurement_frame below.
            "area_m2_approx": round(area_px * pixel_m2, 1),
            "rectangularity": round(rectangularity, 3),
            "rect_angle_deg": round(float(angle) % 90.0, 1),
            "internal_texture": round(interior_texture, 1),
            "periodicity": round(_row_periodicity(component, float(angle)), 4),
        }
        if rectangularity < rect_min or interior_texture < texture_min:
            # Kept in the record: "we saw a large dark rectangle and rejected it, here is why" is
            # more useful to a reviewer than silence.
            record["rejected_because"] = (
                "not_rectangular_enough" if rectangularity < rect_min else "interior_too_smooth"
            )
            rejected.append(record)
            continue
        kept_px += area_px
        clusters.append(record)

    coverage = kept_px / roof_px
    angles = [c["rect_angle_deg"] for c in clusters]
    spread = _angle_spread(angles)
    best_periodicity = max((c["periodicity"] for c in clusters), default=0.0)
    quality.update(
        {
            "n_clusters": len(clusters),
            "coverage_fraction": round(coverage, 4),
            "clusters": sorted(clusters, key=lambda c: -c["area_m2_approx"])[:5],
            "rejected_clusters": sorted(rejected, key=lambda c: -c["area_m2_approx"])[:5],
            "cluster_area_measurement_frame": "local_ground_scale_cos_latitude",
            "cluster_area_source_crs": "EPSG:3857",
            "cluster_area_note": (
                "cluster and rejected-cluster area_m2_approx are diagnostics measured on the image "
                "pixel grid in the approximate local ground-scale frame, not in EPSG:31256. No "
                "published attribute value is derived from them; attributes.roof_area_m2 is the "
                "only published area and is computed in EPSG:31256"
            ),
            "cluster_angle_spread_deg": None if spread is None else round(spread, 1),
            "best_cluster_periodicity": round(best_periodicity, 4),
        }
    )

    # Two independent routes to "regularly spaced", either of which is sufficient: several
    # co-oriented rectangular clusters (an array split by walkways), or one cluster whose
    # internal row pitch is periodic. Coverage is required in both cases.
    co_oriented = len(clusters) >= 2 and spread is not None and spread <= orientation_tol
    periodic = best_periodicity >= periodicity_min
    detected = coverage >= min_coverage and (co_oriented or periodic)
    quality["regularity_evidence"] = {"co_oriented_clusters": co_oriented, "periodic": periodic}

    value, fragment = resolve_asymmetric_detection(detected, shadow_fraction, abstain_above)
    if detected:
        fragment = (
            f"{len(clusters)} dark low-saturation rectangular cluster(s) covering "
            f"{coverage:.1%} of the roof (threshold {min_coverage:.0%}), "
            + (
                f"co-oriented within {spread:.0f} deg"
                if co_oriented
                else f"with a periodic row spacing scoring {best_periodicity:.2f}"
            )
        )
    return _observation(value, fragment, quality)


def _angle_spread(angles: list[float]) -> float | None:
    """Widest disagreement among rectangle angles, on the 90 deg circle rectangles live on."""
    if len(angles) < 2:
        return None
    worst = 0.0
    for i, a in enumerate(angles):
        for b in angles[i + 1 :]:
            diff = abs(a - b) % 90.0
            worst = max(worst, min(diff, 90.0 - diff))
    return worst


def _observation(value: bool | None, fragment: str, quality: dict[str, Any]) -> ImageObservation:
    return ImageObservation(
        name="solar_panels",
        value=value,
        method=METHOD,
        rationale=f"visible solar modules over the authoritative roof outline: {fragment}",
        quality=quality,
        limitations=LIMITATIONS,
    )
