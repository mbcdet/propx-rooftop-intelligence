"""Green-roof detection from an RGB vegetation index — and why it is expected to under-detect.

Two source-side facts govern this module, and the second is the larger (design 5.2,
study_area_selection section 1):

1. There is no NIR band, so no NDVI. ExG and VARI are weaker proxies that respond to *greenness*
   rather than to chlorophyll.
2. **The imagery was flown 19/20 March, 29 March and 12 April 2024. Vegetation is dormant.** An
   extensive sedum roof reads reddish-brown at that time of year, not green. So an RGB index will
   under-detect green roofs on exactly the buildings that have them.

The two visual candidates from reconnaissance — vie-swv-002 (OBJECTID 358722) and vie-swv-006
(242275) — are **candidates, not ground truth**. Nothing here is tuned to make them come out
``"detected"``, no threshold in this module or in ``configs/pipeline.yaml`` was fitted to them,
and this module contains no per-building special case. If the index says ``"not_detected"`` or
abstains on them, that is the correct output and it is reported as a finding about the source
and the season. Loosening ``exg_threshold`` until the expected answer appeared would be fitting
a detector to two eyeballed guesses and then reporting the result as detection.

The value domain is ``"detected"`` / ``"not_detected"`` / ``None`` (unknown), not a Boolean:
``"not_detected"`` is a statement about this imagery on a judgeable roof, never a claim that the
roof has no vegetation.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..imaging import no_data_mask, shadow_mask, shadow_stats, threshold
from ..types import ImageCrop, ImageObservation
from . import resolve_asymmetric_detection

METHOD = "rgb_vegetation_index_exg_with_vari_cross_check"

LIMITATIONS = (
    "no NIR band, so no NDVI; ExG and VARI respond to greenness, not to chlorophyll",
    "imagery flown 19/20 March, 29 March and 12 April 2024 with DORMANT vegetation: an "
    "extensive sedum roof reads reddish-brown, so under-detection is expected and a negative or "
    "abstaining result on a real green roof is a known failure mode (design 5.2)",
    "unknown is an accepted output; the two reconnaissance candidates are visual candidates and "
    "were not used to calibrate any threshold",
    "a negative result is absence of evidence, not evidence of absence (design 6.1)",
    "value domain is detected / not_detected / unknown: not_detected describes this imagery "
    "over a judgeable roof and is never a claim that the roof has no vegetation",
    "not_detected additionally requires the imaged, unshadowed share of the outline to reach "
    "the configured negative judgeability floor; below it the observation abstains, because "
    "the unread share of the roof could hold the vegetation the negative would deny",
)


def _indices(crop: ImageCrop) -> tuple[np.ndarray, np.ndarray]:
    """Excess Green and VARI, both from RGB only.

    ExG is normalised by the pixel's total intensity so it is comparable between a bright plane
    and a dim one. VARI's denominator can approach zero on near-neutral surfaces, so it is
    clipped and reported as a cross-check rather than used for the decision.
    """
    rgb = crop.rgb.astype(np.float32)
    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    exg = (2.0 * green - red - blue) / np.clip(red + green + blue, 1.0, None)
    vari = (green - red) / np.where(np.abs(green + red - blue) < 1.0, 1.0, green + red - blue)
    return exg, np.clip(vari, -1.0, 1.0)


def observe_green_roof(crop: ImageCrop, cfg: Any) -> ImageObservation:
    """Decide whether the roof surface reads as vegetated in the 2024 orthophoto.

    Three values, and absence of evidence is never one of them (RTI-009 / design 6.1):

    * ``"detected"`` when the vegetated share of the roof reaches
      ``image.green_roof.min_coverage_fraction``;
    * ``"not_detected"`` only when it does not *and* the roof is sufficiently judgeable for
      vegetation — the vegetation-specific judgeability is the unshadowed, imaged share of the
      outline, and it must clear both the whole-roof abstention threshold and
      ``image.green_roof.negative_min_judgeable_fraction``, the negative's own floor (set
      equal to the documented review-quality floor: a negative resting on less of the roof
      than that is not defensible even as a hedged statement about this imagery);
    * ``None`` (unknown) when nothing was detected but the roof is too shadowed or too poorly
      imaged to support a negative claim.

    The value is deliberately a string, not a Boolean: ``False`` reads as "there is no green
    roof", which the dormant-season RGB evidence cannot establish. ``"not_detected"`` states
    exactly what was measured. In-season limits stay in ``limitations``.
    """
    exg_min = float(threshold(cfg, "image", "green_roof", "exg_threshold"))
    min_coverage = float(threshold(cfg, "image", "green_roof", "min_coverage_fraction"))
    abstain_above = float(threshold(cfg, "image", "shadow_fraction_abstain"))
    negative_floor = float(
        threshold(cfg, "image", "green_roof", "negative_min_judgeable_fraction")
    )
    quality, coverage, lit_coverage = _evidence(crop, cfg, exg_min)
    quality["thresholds"] = {
        "exg_threshold": exg_min,
        "min_coverage_fraction": min_coverage,
        "shadow_fraction_abstain": abstain_above,
        "negative_min_judgeable_fraction": negative_floor,
    }
    shadow_fraction = quality.get("shadow_fraction")
    # Vegetation-specific judgeability: the share of the outline that is imaged and unshadowed
    # (shadow_fraction counts both causes; see imaging.shadow_stats). Published so the negative
    # is auditable against the gate that permitted it.
    vegetation_judgeable = (
        None if shadow_fraction is None else round(1.0 - float(shadow_fraction), 4)
    )
    quality["vegetation_judgeable_fraction"] = vegetation_judgeable

    detected = coverage is not None and coverage >= min_coverage
    resolved, fragment = resolve_asymmetric_detection(detected, shadow_fraction, abstain_above)
    if resolved is False and (
        vegetation_judgeable is None or vegetation_judgeable < negative_floor
    ):
        # The negative's own judgeability floor (a general threshold, never a building
        # exception): nothing was detected and the whole-roof abstention gate passed, but the
        # imaged-and-unshadowed share of the outline is below the documented review-quality
        # floor. On that little evidence even a hedged not_detected is not defensible — the
        # unread share of the roof could hold the vegetated coverage the verdict would deny —
        # so the observation abstains. The raw index diagnostics stay published in quality.
        resolved = None
        judgeable_text = (
            "an unmeasurable share"
            if vegetation_judgeable is None
            else f"only {vegetation_judgeable:.1%}"
        )
        fragment = (
            f"abstained: nothing detected, but {judgeable_text} of the roof outline is imaged "
            f"and unshadowed, below the {negative_floor:.0%} vegetation judgeability floor a "
            f"negative requires; that low a judgeable share prevents even a defensible "
            f"not_detected observation, because the unread part of the roof could hold the "
            f"vegetated coverage the negative would deny"
        )
    value = {True: "detected", False: "not_detected", None: None}[resolved]
    if detected:
        lit_text = "no lit pixels" if lit_coverage is None else f"{lit_coverage:.1%} of lit pixels"
        fragment = (
            f"ExG exceeds {exg_min} on {coverage:.1%} of roof pixels "
            f"({lit_text}), at or above the {min_coverage:.0%} threshold"
        )
    elif value == "not_detected" and coverage is not None:
        fragment = (
            f"ExG exceeds {exg_min} on only {coverage:.1%} of roof pixels, below the "
            f"{min_coverage:.0%} threshold; {fragment}. Note that dormant March/April "
            f"vegetation reads reddish-brown, so this does not rule out an extensive green roof"
        )
    return ImageObservation(
        name="green_roof",
        value=value,
        method=METHOD,
        rationale=f"RGB vegetation index over the authoritative roof outline: {fragment}",
        quality=quality,
        limitations=LIMITATIONS,
    )


def observe_vegetated_fraction(crop: ImageCrop, cfg: Any) -> ImageObservation:
    """The measured vegetated share of the roof, as a standalone observation.

    A continuous measurement, so it is reported whatever it is — including 0.0, which is a
    measurement rather than an absence. ``None`` only when there are no roof pixels to measure.

    **Not wired into the published document.** The same figure already ships as
    ``green_roof``'s ``image_evidence.quality.vegetated_fraction`` (see :func:`_evidence`), so
    the pipeline does not call this and no attribute is built from it. It stays because it is
    the accessor a future consumer would want if the fraction is ever promoted to an attribute
    of its own, and because the tests measure the fraction through it directly.
    """
    exg_min = float(threshold(cfg, "image", "green_roof", "exg_threshold"))
    quality, coverage, _ = _evidence(crop, cfg, exg_min)
    quality["thresholds"] = {"exg_threshold": exg_min}
    return ImageObservation(
        name="vegetated_fraction",
        value=None if coverage is None else round(coverage, 4),
        method=METHOD,
        rationale=(
            "share of roof-mask pixels whose Excess Green index exceeds the configured "
            "threshold. Measured on dormant early-spring imagery, so this is a lower bound on "
            "any real vegetated area"
            if coverage is not None
            else "no roof pixels inside the crop, so no fraction could be measured"
        ),
        quality=quality,
        limitations=LIMITATIONS,
    )


def _evidence(
    crop: ImageCrop, cfg: Any, exg_min: float
) -> tuple[dict[str, Any], float | None, float | None]:
    """Shared index statistics: (quality, coverage over the roof, coverage over lit pixels)."""
    quality: dict[str, Any] = dict(shadow_stats(crop, cfg))
    # Pixels with no cached imagery carry no index value; counting them in the denominator would
    # dilute a real coverage fraction towards a false negative.
    roof = np.asarray(crop.roof_mask, dtype=bool) & ~no_data_mask(crop)
    roof_px = int(roof.sum())
    quality["roof_pixels_with_imagery"] = roof_px
    if roof_px == 0:
        return quality, None, None

    exg, vari = _indices(crop)
    # "Lit" by the same adaptive shadow detection every judgeability figure uses, so the
    # lit-only fraction and the published shadow_fraction cannot disagree about what shadow is.
    lit = roof & ~shadow_mask(crop, cfg)
    vegetated = roof & (exg >= exg_min)
    coverage = float(vegetated.sum()) / roof_px
    lit_px = int(lit.sum())
    lit_coverage = float((vegetated & lit).sum()) / lit_px if lit_px else None

    quality.update(
        {
            # The decision uses the whole-roof denominator; the lit-only figure is reported so a
            # partly shadowed roof can be read without recomputing anything.
            "vegetated_fraction": round(coverage, 4),
            "vegetated_fraction_lit_pixels": (
                None if lit_coverage is None else round(lit_coverage, 4)
            ),
            "mean_exg": round(float(exg[roof].mean()), 4),
            "p95_exg": round(float(np.percentile(exg[roof], 95)), 4),
            "mean_vari": round(float(vari[roof].mean()), 4),
            "index": "ExG = (2G - R - B) / (R + G + B); VARI = (G - R) / (G + R - B)",
        }
    )
    return quality, coverage, lit_coverage
