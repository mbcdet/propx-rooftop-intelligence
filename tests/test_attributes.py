"""Image observations: surface class, orientation, solar arrays, vegetation.

The asymmetry of design 6.1 is the load-bearing behaviour here — nothing detected over a shadowed
roof must abstain (``None``) rather than report ``False`` — so it is tested on both detectors.
"""

from __future__ import annotations

import numpy as np
import pytest
from test_imaging import CFG, square_wgs84, synthetic_crop

from propx_roofs.attributes import (
    orientation,
    resolve_asymmetric_detection,
    roof_form,
    solar,
    surface,
    vegetation,
)
from propx_roofs.types import ImageObservation

SHADOW_LIMIT = float(CFG.threshold("image", "shadow_luminance_threshold"))
ABSTAIN_ABOVE = float(CFG.threshold("image", "shadow_fraction_abstain"))


def grainy(base: tuple[int, int, int], shape: tuple[int, int], spread: int = 5) -> np.ndarray:
    """A uniform colour with light grain, standing in for a JPEG orthophoto surface."""
    rng = np.random.default_rng(11)
    rgb = np.zeros((*shape, 3), np.int16)
    rgb[:] = base
    return np.clip(rgb + rng.integers(-spread, spread + 1, (*shape, 3)), 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------------------------
# the asymmetry rule itself (design 6.1)


def test_asymmetric_rule_abstains_under_shadow_and_never_invents_a_negative() -> None:
    assert resolve_asymmetric_detection(True, 0.9, ABSTAIN_ABOVE)[0] is True
    assert resolve_asymmetric_detection(False, 0.0, ABSTAIN_ABOVE)[0] is False
    assert resolve_asymmetric_detection(False, ABSTAIN_ABOVE + 0.01, ABSTAIN_ABOVE)[0] is None
    assert resolve_asymmetric_detection(False, None, ABSTAIN_ABOVE)[0] is None
    assert "absence of evidence" in resolve_asymmetric_detection(False, 0.0, ABSTAIN_ABOVE)[1]


def test_the_negative_rationale_reports_the_measured_shadow_fraction_not_a_literal() -> None:
    """The rationale must quote the fraction it was given, not the one this dataset happens to have.

    Every selected building measures 0.0%, so a hardcoded "0 percent" was indistinguishable from
    a measurement here and would have silently lied on the first darker roof. A fraction below
    the abstention threshold still returns False — that behaviour is unchanged; only the text is.
    """
    fraction = ABSTAIN_ABOVE / 2  # 17.5% with the shipped 0.35 threshold: below the gate
    assert fraction < ABSTAIN_ABOVE
    value, rationale = resolve_asymmetric_detection(False, fraction, ABSTAIN_ABOVE)

    assert value is False, "a fraction below the gate must still yield a negative, not abstention"
    assert f"{fraction:.1%}" in rationale
    assert "0.0%" not in rationale
    assert "0 percent" not in rationale
    # The gate it was tested against is named too, so the number is interpretable.
    assert f"{ABSTAIN_ABOVE:.0%}" in rationale

    # ...and the real-data case still reads 0.0%, which is the honest measurement there.
    zero_value, zero_rationale = resolve_asymmetric_detection(False, 0.0, ABSTAIN_ABOVE)
    assert zero_value is False
    assert "0.0%" in zero_rationale


# --------------------------------------------------------------------------------------------
# surface class


def test_surface_appearance_names_an_appearance_not_a_material() -> None:
    crop = synthetic_crop(grainy((190, 190, 195), (200, 200), spread=2))
    observation = surface.observe_surface_appearance(crop, CFG)
    assert isinstance(observation, ImageObservation)
    assert observation.name == "visual_surface_appearance"
    assert observation.value == "bright_smooth_like"  # smooth, bright, near-neutral
    assert "never a claim about construction material" in observation.rationale
    assert any("cannot establish construction material" in lim for lim in observation.limitations)
    # The dominance evidence is machine-readable.
    assert observation.quality["dominance_share"] > 0.9
    assert observation.quality["margin_over_runner_up"] > 0.5
    assert "unclassified_share" in observation.quality


def test_surface_appearance_detects_vegetation_and_tile_signatures() -> None:
    green = synthetic_crop(grainy((70, 140, 60), (200, 200)))
    assert surface.observe_surface_appearance(green, CFG).value == "vegetated_like"

    # Warm, saturated and rough: fired-clay appearance with visible courses.
    tiled = grainy((165, 105, 80), (200, 200), spread=3)
    tiled[::6, :] = (110, 70, 55)  # tile courses every ~0.6 m
    assert surface.observe_surface_appearance(synthetic_crop(tiled), CFG).value == "tiled_like"


def test_surface_appearance_reports_mixed_and_abstains_without_pixels() -> None:
    half = np.zeros((200, 200, 3), np.uint8)
    half[:, :100] = grainy((165, 105, 80), (200, 100), spread=3)[:, :100]
    half[::6, :100] = (110, 70, 55)
    rough = grainy((150, 150, 152), (200, 100), spread=3)
    rough[:, ::4] = (95, 95, 97)  # gravel speckle
    half[:, 100:] = rough
    observation = surface.observe_surface_appearance(synthetic_crop(half), CFG)
    assert observation.value in {"mixed", "tiled_like", None}

    empty = synthetic_crop(grainy((180, 180, 180), (50, 50)), mask=np.zeros((50, 50), bool))
    observation = surface.observe_surface_appearance(empty, CFG)
    assert observation.value is None
    assert "no judgeable roof interior" in observation.rationale


def test_surface_appearance_abstains_when_too_little_of_the_roof_is_judgeable() -> None:
    """A mostly shadowed roof must abstain rather than classify its lit sliver (RTI-017)."""
    rgb = grainy((40, 40, 42), (200, 200), spread=3)  # below the absolute shadow floor
    rgb[:, :40] = grainy((190, 190, 193), (200, 40), spread=2)  # a lit strip of 20%
    observation = surface.observe_surface_appearance(synthetic_crop(rgb), CFG)
    assert observation.value is None
    assert "judgeable" in observation.rationale
    min_judgeable = float(CFG.threshold("image", "surface", "min_judgeable_fraction"))
    assert observation.quality["judgeable_fraction"] < min_judgeable


def test_surface_appearance_abstains_on_a_weak_dominance_margin() -> None:
    """A leading class within the resolvable margin of the runner-up is noise wearing a label.

    The margin threshold is pinned high in a config copy so the test states its own premise
    (the mechanism, not the shipped number): ~72% bright-smooth against ~28% vegetated clears
    dominance but not a 0.6 margin, and the observation must abstain rather than publish.
    """
    from test_imaging import override

    rgb = np.zeros((200, 200, 3), np.uint8)
    rgb[:, :56] = grainy((70, 140, 60), (200, 56))[:, :56]  # vegetated share
    rgb[:, 56:] = grainy((190, 190, 193), (200, 144), spread=2)[:, :144]
    strict = override(CFG, ("image", "surface", "min_dominance_margin"), 0.6)
    observation = surface.observe_surface_appearance(synthetic_crop(rgb), strict)
    assert observation.value is None
    assert "margin" in observation.rationale
    quality = observation.quality
    assert quality["dominance_share"] >= quality["thresholds"]["dominant_share_min"]
    assert quality["margin_over_runner_up"] < 0.6

    # ...and with the shipped margin the same roof publishes, so the abstention really came
    # from the margin gate and not from the imagery.
    shipped = surface.observe_surface_appearance(synthetic_crop(rgb), CFG)
    assert shipped.value == "bright_smooth_like"


# --------------------------------------------------------------------------------------------
# orientation


def test_ridge_observation_is_none_when_no_ridge_exists() -> None:
    """A featureless bright roof has no ridge, and the field must be absent, not guessed."""
    crop = synthetic_crop(grainy((185, 185, 188), (300, 300), spread=3))
    observation = orientation.observe_ridge_orientation(crop, CFG)
    assert observation.value is None
    assert "no ridge line detected" in observation.rationale
    assert any("absence is not evidence of a flat roof" in lim for lim in observation.limitations)


def test_ridge_is_reported_only_with_line_agreement_and_plane_contrast() -> None:
    """Two planes of different brightness meeting on a line: a gable seen from above."""
    size = 400
    rgb = np.zeros((size, size, 3), np.int16)
    rgb[:, : size // 2] = (110, 108, 105)  # shaded plane
    rgb[:, size // 2 :] = (190, 188, 185)  # sunlit plane
    rgb[:, size // 2 - 3 : size // 2 + 3] = (240, 238, 235)  # ridge cap, both edges detectable
    rng = np.random.default_rng(5)
    rgb = np.clip(rgb + rng.integers(-4, 5, rgb.shape), 0, 255).astype(np.uint8)
    mask = np.zeros((size, size), bool)
    mask[40:-40, 40:-40] = True

    observation = orientation.observe_ridge_orientation(synthetic_crop(rgb, mask), CFG)
    assert observation.value is not None, observation.rationale
    # The ridge runs north-south in the image, so its grid bearing is near 0/180 degrees.
    assert min(observation.value, 180.0 - observation.value) < 12.0
    assert observation.quality["plane_brightness_contrast"] > 0.15
    assert observation.quality["n_supporting_lines"] >= 2


def test_ridge_rejects_a_co_oriented_line_without_plane_contrast() -> None:
    """A parapet or panel row gives agreeing lines but no brightness step across them."""
    size = 400
    rgb = np.full((size, size, 3), 120, np.int16)
    for x in range(80, 320, 30):  # regular bright lines, same surface on both sides
        rgb[:, x : x + 3] = 255
    rng = np.random.default_rng(5)
    rgb = np.clip(rgb + rng.integers(-4, 5, rgb.shape), 0, 255).astype(np.uint8)
    mask = np.zeros((size, size), bool)
    mask[40:-40, 40:-40] = True
    observation = orientation.observe_ridge_orientation(synthetic_crop(rgb, mask), CFG)
    assert observation.value is None
    assert "brightness" in observation.rationale or "parapet" in observation.rationale


def test_ridge_rejects_a_cast_shadow_boundary() -> None:
    """A cast-shadow edge is a strong brightness step, but its dark flank is classified shadow
    by the adaptive detector, so the flank gate must withhold it (RTI-007)."""
    size = 400
    rgb = np.zeros((size, size, 3), np.int16)
    rgb[:, : size // 2] = (80, 80, 84)  # cast shadow: dark relative to the roof, above 55
    rgb[:, size // 2 :] = (190, 188, 185)
    rng = np.random.default_rng(5)
    rgb = np.clip(rgb + rng.integers(-4, 5, rgb.shape), 0, 255).astype(np.uint8)
    mask = np.zeros((size, size), bool)
    mask[40:-40, 40:-40] = True
    observation = orientation.observe_ridge_orientation(synthetic_crop(rgb, mask), CFG)
    assert observation.value is None
    gates = observation.quality.get("gates")
    if gates is not None:  # a candidate line was found: it must have failed the shadow gate
        assert gates["shadow_flank_overlap"]["passed"] is False or (
            "illumination boundary" in observation.rationale
        )


def test_ridge_rejects_an_off_axis_step_bounding_a_sub_roof() -> None:
    """A terrace/deck/facade step runs off the medial axis of its roof section; a crest runs
    on it. The fixture is an elongated block (~32 x 14 m, the shape such steps occur on) with
    a gable-like brightness step along its long side at ~a quarter of the width: photometry
    identical to a ridge, geometry that of a deck edge."""
    rgb = np.zeros((240, 400, 3), np.int16)
    step_at = 75  # 3.5 m from the north eave of a 14 m deep roof: far off the medial axis
    rgb[:step_at, :] = (110, 108, 105)  # upper strip: lit, above any shadow threshold
    rgb[step_at:, :] = (190, 188, 185)
    rgb[step_at - 3 : step_at + 3, :] = (240, 238, 235)  # coping/edge cap, both edges strong
    rng = np.random.default_rng(5)
    rgb = np.clip(rgb + rng.integers(-4, 5, rgb.shape), 0, 255).astype(np.uint8)
    mask = np.zeros((240, 400), bool)
    mask[40:180, 40:360] = True
    observation = orientation.observe_ridge_orientation(synthetic_crop(rgb, mask), CFG)
    assert observation.value is None, observation.rationale
    gates = observation.quality.get("gates")
    assert gates is not None
    assert gates["medial_axis_ratio"]["passed"] is False
    assert gates["medial_axis_ratio"]["value"] < 0.8
    assert "sub-roof" in observation.rationale


def test_ridge_gates_are_published_machine_readable_on_success() -> None:
    """The accepted ridge must carry every gate value, its threshold and a summary margin."""
    size = 400
    rgb = np.zeros((size, size, 3), np.int16)
    rgb[:, : size // 2] = (110, 108, 105)
    rgb[:, size // 2 :] = (190, 188, 185)
    rgb[:, size // 2 - 3 : size // 2 + 3] = (240, 238, 235)
    rng = np.random.default_rng(5)
    rgb = np.clip(rgb + rng.integers(-4, 5, rgb.shape), 0, 255).astype(np.uint8)
    mask = np.zeros((size, size), bool)
    mask[40:-40, 40:-40] = True
    observation = orientation.observe_ridge_orientation(synthetic_crop(rgb, mask), CFG)
    assert observation.value is not None, observation.rationale
    gates = observation.quality["gates"]
    for gate in ("plane_contrast", "shadow_flank_overlap", "medial_axis_ratio",
                 "axis_alignment_deg"):
        assert gate in gates
    assert gates["plane_contrast"]["passed"] is True
    assert gates["shadow_flank_overlap"]["passed"] is True
    assert gates["medial_axis_ratio"]["passed"] is True
    assert gates["medial_axis_ratio"]["value"] > 0.8
    # No geometry was supplied, so the axis gate reports not-evaluated rather than a verdict.
    assert gates["axis_alignment_deg"]["value"] is None
    assert observation.quality["gate_margin"] > 0


def test_footprint_axis_is_derived_and_separate_from_the_ridge() -> None:
    import math

    lat, lon = 48.1865, 16.3790
    d_lat = 8.0 / 110574.0
    d_lon = 40.0 / (111320.0 * math.cos(math.radians(lat)))
    from shapely.geometry import Polygon

    wide = Polygon(
        [
            (lon - d_lon, lat - d_lat),
            (lon + d_lon, lat - d_lat),
            (lon + d_lon, lat + d_lat),
            (lon - d_lon, lat + d_lat),
        ]
    )
    observation = orientation.observe_footprint_axis(wide, CFG)
    assert observation.name == "footprint_axis_orientation_deg"
    assert observation.value == pytest.approx(90.0, abs=2.0)  # long axis runs east-west
    assert observation.quality["long_side_m"] > observation.quality["short_side_m"]
    assert any("NOT a roof slope aspect" in lim for lim in observation.limitations)

    square = orientation.observe_footprint_axis(square_wgs84(15.0), CFG)
    assert square.quality["weakly_defined"] is True

    ridge, axis = orientation.observe(synthetic_crop(grainy((185, 185, 188), (200, 200))), wide,
                                      CFG)
    assert (ridge.name, axis.name) == ("ridge_orientation_deg", "footprint_axis_orientation_deg")


# --------------------------------------------------------------------------------------------
# solar


def panel_array(size: int = 400) -> tuple[np.ndarray, np.ndarray]:
    """A bright roof carrying two dark, framed, regularly spaced module blocks."""
    rgb = grainy((185, 185, 188), (size, size), spread=3).astype(np.int16)
    for x0 in (60, 230):
        block = np.zeros((160, 130, 3), np.int16)
        block[:] = (95, 98, 105)  # dark, near-neutral, slightly blue
        block[::10, :] = (150, 152, 158)  # module frames every ~1 m
        block[:, ::13] = (150, 152, 158)
        rgb[80:240, x0 : x0 + 130] = block
    rng = np.random.default_rng(2)
    rgb = np.clip(rgb + rng.integers(-3, 4, rgb.shape), 0, 255).astype(np.uint8)
    mask = np.zeros((size, size), bool)
    mask[30:-30, 30:-30] = True
    return rgb, mask


def test_solar_detects_a_regular_dark_module_grid() -> None:
    rgb, mask = panel_array()
    observation = solar.observe_solar_panels(synthetic_crop(rgb, mask), CFG)
    assert observation.value is True, observation.rationale
    assert observation.quality["n_clusters"] >= 2
    assert observation.quality["coverage_fraction"] >= CFG.threshold(
        "image", "solar_panels", "min_coverage_fraction"
    )
    from propx_roofs.attributes import param

    assert observation.quality["clusters"][0]["internal_texture"] >= param(
        CFG, "solar_internal_texture_min"
    )


def test_solar_reports_false_on_a_well_lit_roof_with_nothing_on_it() -> None:
    crop = synthetic_crop(grainy((185, 185, 188), (300, 300), spread=3))
    observation = solar.observe_solar_panels(crop, CFG)
    assert observation.value is False
    assert "absence of evidence" in observation.rationale
    assert observation.quality["shadow_fraction"] == 0.0


def test_solar_rejects_a_smooth_dark_patch_that_is_not_an_array() -> None:
    """A shaded roof plane is dark, neutral and rectangular. Only its interior is smooth."""
    rgb = grainy((185, 185, 188), (400, 400), spread=3)
    rgb[80:300, 80:300] = grainy((95, 96, 100), (220, 220), spread=3)
    mask = np.zeros((400, 400), bool)
    mask[30:-30, 30:-30] = True
    observation = solar.observe_solar_panels(synthetic_crop(rgb, mask), CFG)
    assert observation.value is False
    assert observation.quality["n_clusters"] == 0
    assert observation.quality["rejected_clusters"], "the rejection should be recorded, not silent"
    assert observation.quality["rejected_clusters"][0]["rejected_because"] == "interior_too_smooth"


def test_solar_abstains_rather_than_reporting_false_under_heavy_shadow() -> None:
    dark = synthetic_crop(grainy((30, 30, 34), (300, 300), spread=4))
    observation = solar.observe_solar_panels(dark, CFG)
    assert observation.value is None, "a shadowed roof cannot support a negative claim"
    assert observation.quality["shadow_fraction"] > ABSTAIN_ABOVE
    assert "abstained" in observation.rationale


# --------------------------------------------------------------------------------------------
# vegetation


def test_green_roof_detects_an_unambiguously_green_surface() -> None:
    rgb = grainy((180, 180, 182), (300, 300), spread=3)
    rgb[:150, :] = grainy((70, 150, 60), (150, 300))  # half the roof clearly green
    observation = vegetation.observe_green_roof(synthetic_crop(rgb), CFG)
    assert observation.value == "detected"
    assert observation.quality["vegetated_fraction"] > CFG.threshold(
        "image", "green_roof", "min_coverage_fraction"
    )


def test_green_roof_reports_not_detected_on_dormant_reddish_brown_substrate() -> None:
    """The expected outcome on a March/April sedum roof — and it must be stated, not hidden.

    The value is the string "not_detected", never Boolean False (RTI-009): a Boolean read as
    "there is no green roof", which the dormant-season evidence cannot establish.
    """
    crop = synthetic_crop(grainy((150, 120, 105), (300, 300), spread=4))
    observation = vegetation.observe_green_roof(crop, CFG)
    assert observation.value == "not_detected"
    assert not isinstance(observation.value, bool)
    assert observation.quality["vegetated_fraction"] < 0.01
    # The vegetation-specific judgeability that permitted the negative is published.
    assert observation.quality["vegetation_judgeable_fraction"] >= 1.0 - ABSTAIN_ABOVE
    assert "dormant March/April vegetation" in observation.rationale
    assert any("DORMANT" in lim for lim in observation.limitations)


def test_green_roof_negative_requires_the_judgeability_floor_not_only_the_abstention_gate() -> None:
    """The band between the whole-roof abstention gate and the negative's own judgeability
    floor: readable enough to look at, but a negative there would rest on less of the roof
    than the documented review-quality floor, so the honest output is unknown. A general
    threshold, not a building exception — asserted here on synthetic pixels only."""
    floor = float(CFG.threshold("image", "green_roof", "negative_min_judgeable_fraction"))
    # The floor dominates the abstention gate for negatives, and is deliberately set equal to
    # the review-quality floor: below it a negative would immediately need review anyway.
    assert floor > 1.0 - ABSTAIN_ABOVE
    assert floor == float(CFG.threshold("review", "vegetation_judgeable_review"))

    rgb = grainy((190, 190, 192), (300, 300), spread=3)
    rgb[:, :90] = grainy((80, 80, 82), (300, 90), spread=3)[:, :90]  # ~30% cast shadow
    observation = vegetation.observe_green_roof(synthetic_crop(rgb), CFG)
    judgeable = observation.quality["vegetation_judgeable_fraction"]
    assert 1.0 - ABSTAIN_ABOVE <= judgeable < floor, "the crop must land inside the band"
    assert observation.value is None
    assert "judgeability floor" in observation.rationale
    assert "not_detected" in observation.rationale  # says what the low judgeability prevents
    # The raw index diagnostics stay published even though the verdict abstained.
    assert observation.quality["vegetated_fraction"] is not None
    assert observation.quality["mean_exg"] is not None
    assert observation.quality["thresholds"]["negative_min_judgeable_fraction"] == floor


def test_green_roof_negative_publishes_at_or_above_the_judgeability_floor() -> None:
    """The other side of the boundary: a lightly shadowed but well-judgeable roof still earns
    an honest not_detected — the floor must not swallow every negative."""
    floor = float(CFG.threshold("image", "green_roof", "negative_min_judgeable_fraction"))
    rgb = grainy((190, 190, 192), (300, 300), spread=3)
    rgb[:, :36] = grainy((80, 80, 82), (300, 36), spread=3)[:, :36]  # ~12% cast shadow
    observation = vegetation.observe_green_roof(synthetic_crop(rgb), CFG)
    assert observation.quality["vegetation_judgeable_fraction"] >= floor
    assert observation.value == "not_detected"


def test_green_roof_abstains_under_heavy_shadow() -> None:
    """Low vegetation judgeability yields unknown (None), never "not_detected" (RTI-009)."""
    dark = synthetic_crop(grainy((32, 30, 32), (300, 300), spread=4))
    observation = vegetation.observe_green_roof(dark, CFG)
    assert observation.value is None
    assert observation.quality["shadow_fraction"] > ABSTAIN_ABOVE
    assert observation.quality["vegetation_judgeable_fraction"] < 1.0 - ABSTAIN_ABOVE


def test_green_roof_abstains_on_a_relatively_shadowed_roof_above_the_absolute_floor() -> None:
    """The RTI-006 regression at the observation level: cast shadow at luminance ~80 is far
    above the absolute floor, and a roof mostly covered by it cannot support a negative."""
    rgb = grainy((190, 190, 192), (300, 300), spread=3)
    rgb[:, :180] = grainy((80, 80, 82), (300, 180), spread=3)[:, :180]  # 60% cast shadow
    observation = vegetation.observe_green_roof(synthetic_crop(rgb), CFG)
    assert observation.value is None
    assert observation.quality["shadow_fraction"] > ABSTAIN_ABOVE


def test_vegetated_fraction_is_measured_even_when_it_is_zero() -> None:
    crop = synthetic_crop(grainy((150, 120, 105), (200, 200), spread=4))
    observation = vegetation.observe_vegetated_fraction(crop, CFG)
    assert observation.name == "vegetated_fraction"
    assert observation.value == pytest.approx(0.0, abs=0.01)
    assert observation.value is not None, "a measured zero is not the same as an abstention"


def test_no_detector_returns_an_attribute_object() -> None:
    """Provenance and confidence belong to another stage; this one only observes."""
    crop = synthetic_crop(grainy((185, 185, 188), (200, 200), spread=3))
    for observation in (
        surface.observe_surface_appearance(crop, CFG),
        orientation.observe_ridge_orientation(crop, CFG),
        orientation.observe_footprint_axis(square_wgs84(15.0), CFG),
        solar.observe_solar_panels(crop, CFG),
        vegetation.observe_green_roof(crop, CFG),
        vegetation.observe_vegetated_fraction(crop, CFG),
        roof_form.observe_roof_form(crop, CFG),
    ):
        assert isinstance(observation, ImageObservation)
        assert set(observation.as_dict()) == {
            "value",
            "method",
            "rationale",
            "quality",
            "limitations",
        }


# --------------------------------------------------------------------------------------------
# roof form (RTI-005): symmetric flat / pitched / unknown


def test_roof_form_reports_flat_only_on_positive_evidence() -> None:
    """A clean, judgeable, single-brightness-mode roof is positive evidence of flatness."""
    crop = synthetic_crop(grainy((185, 185, 188), (300, 300), spread=3))
    observation = roof_form.observe_roof_form(crop, CFG)
    assert observation.value == "flat"
    assert observation.quality["margin"] > 0
    features = observation.quality["features"]
    assert features["otsu_plane_contrast"] < CFG.threshold(
        "image", "roof_form", "flat_plane_contrast_max"
    )
    assert "positive flatness evidence" in observation.rationale


def test_roof_form_reports_pitched_on_a_gated_ridge() -> None:
    size = 400
    rgb = np.zeros((size, size, 3), np.int16)
    rgb[:, : size // 2] = (110, 108, 105)
    rgb[:, size // 2 :] = (190, 188, 185)
    rgb[:, size // 2 - 3 : size // 2 + 3] = (240, 238, 235)
    rng = np.random.default_rng(5)
    rgb = np.clip(rgb + rng.integers(-4, 5, rgb.shape), 0, 255).astype(np.uint8)
    mask = np.zeros((size, size), bool)
    mask[40:-40, 40:-40] = True
    observation = roof_form.observe_roof_form(synthetic_crop(rgb, mask), CFG)
    assert observation.value == "pitched", observation.rationale
    assert observation.quality["ridge_observation"]["value"] is not None
    assert observation.quality["margin"] is not None


def test_roof_form_abstains_on_shadow_and_on_two_modes_without_a_ridge() -> None:
    """Neither a shadowed roof nor an ambiguous two-mode roof supports either claim."""
    dark = synthetic_crop(grainy((32, 30, 32), (300, 300), spread=4))
    shadowed = roof_form.observe_roof_form(dark, CFG)
    assert shadowed.value is None
    assert "judgeable" in shadowed.rationale

    # Two strong lit brightness modes, mixed as a checkerboard of large patches: brightness
    # says "not flat" but no line can pass the ridge gates, so the only honest answer is None.
    rgb = grainy((190, 190, 192), (300, 300), spread=3)
    for i in range(0, 300, 60):
        for j in range(0, 300, 60):
            if (i // 60 + j // 60) % 2 == 0:
                rgb[i : i + 60, j : j + 60] = grainy((120, 120, 122), (60, 60), spread=3)
    ambiguous = roof_form.observe_roof_form(synthetic_crop(rgb), CFG)
    assert ambiguous.value is None
    assert "neither flat nor pitched" in ambiguous.rationale
    assert ambiguous.quality["features"]["otsu_plane_contrast"] is not None


def test_roof_form_never_maps_a_missed_ridge_to_flat_under_low_judgeability() -> None:
    """The one-directional failure RTI-005 exists to prevent, in reverse: a shadowed pitched
    roof must not read as flat merely because its ridge was not detected."""
    size = 300
    rgb = grainy((190, 190, 192), (size, size), spread=3)
    rgb[:, : int(size * 0.55)] = grainy((80, 80, 82), (size, int(size * 0.55)), spread=3)
    observation = roof_form.observe_roof_form(synthetic_crop(rgb), CFG)
    assert observation.value is None
    assert observation.quality["judgeable_fraction"] < CFG.threshold(
        "image", "roof_form", "min_judgeable_fraction"
    )


# --------------------------------------------------------------------------------------------
# real-cache regressions: the committed imagery, general thresholds, pinned fixtures
#
# The building ids below are regression FIXTURES. Every threshold involved is a general,
# documented principle in configs/pipeline.yaml or ATTRIBUTE_PARAMS; nothing in src/ mentions
# any building id, and these tests assert what the general gates measure on the real cache.

from test_imaging import cached_crop, usable_tile_cache  # noqa: E402

needs_cache = pytest.mark.skipif(
    usable_tile_cache() is None, reason="no readable tiles in the committed cache"
)


def _cached_geometry(building_id: str):
    import json

    cache = usable_tile_cache()
    records = json.loads((cache / "roof_records_2025.geojson").read_text(encoding="utf-8"))
    by_id = {f["properties"]["OBJECTID"]: f for f in records["features"]}
    building = next(b for b in CFG.study_area.buildings if b.building_id == building_id)
    return by_id[building.objectid]["geometry"]


@needs_cache
def test_real_cache_ridge_survives_on_007_and_is_withheld_on_008_and_010() -> None:
    """RTI-007 on the committed imagery: the one visually plausible ridge (007) passes every
    gate; the deck/facade edge on 008 fails the medial-axis gate and the shadow-boundary edge
    on 010 fails the shadow-flank gate. All by the same general thresholds."""
    survived = orientation.observe_ridge_orientation(
        cached_crop("vie-swv-007"), CFG, _cached_geometry("vie-swv-007")
    )
    assert survived.value is not None, survived.rationale
    assert survived.value == pytest.approx(65.0, abs=2.0)
    assert all(gate["passed"] for gate in survived.quality["gates"].values())
    assert survived.quality["gate_margin"] > 0
    # The accepted ridge-support segment travels with the acceptance, and the published
    # azimuth is reproducible from its endpoints — so the overlay draws the observed
    # evidence, never a line synthesised from the angle.
    segment = survived.quality["accepted_segment"]
    (lon0, lat0), (lon1, lat1) = segment["endpoints_lonlat"]
    from propx_roofs.geometry import azimuth_deg

    reproduced = azimuth_deg(
        (lon0, lat0), (lon1, lat1), str(CFG.threshold("crs", "metric"))
    )
    assert reproduced == pytest.approx(survived.value, abs=0.01)

    deck_edge = orientation.observe_ridge_orientation(
        cached_crop("vie-swv-008"), CFG, _cached_geometry("vie-swv-008")
    )
    assert deck_edge.value is None, "008's deck/facade edge must be withheld"
    assert deck_edge.quality["gates"]["medial_axis_ratio"]["passed"] is False
    assert "accepted_segment" not in deck_edge.quality, "a withheld ridge leaves no segment"

    shadow_edge = orientation.observe_ridge_orientation(
        cached_crop("vie-swv-010"), CFG, _cached_geometry("vie-swv-010")
    )
    assert shadow_edge.value is None, "010's shadow-boundary edge must be withheld"
    assert "accepted_segment" not in shadow_edge.quality, "a withheld ridge leaves no segment"
    gates = shadow_edge.quality["gates"]
    assert (
        gates["shadow_flank_overlap"]["passed"] is False
        or gates["medial_axis_ratio"]["passed"] is False
    )


@needs_cache
def test_real_cache_shadowed_roofs_abstain_or_flag_reduced_judgeability() -> None:
    """RTI-006 downstream: material shadow on 008/010 must reach the observations' quality
    dicts and reduce judgeability; the well-lit 001 and 007 are the positive controls."""
    for building_id, min_shadow in (("vie-swv-008", 0.05), ("vie-swv-010", 0.20)):
        observation = surface.observe_surface_appearance(cached_crop(building_id), CFG)
        assert observation.quality["shadow_fraction"] >= min_shadow
        assert observation.quality["judgeable_fraction"] <= 1.0 - min_shadow
    for building_id in ("vie-swv-001", "vie-swv-007"):
        observation = surface.observe_surface_appearance(cached_crop(building_id), CFG)
        assert observation.quality["judgeable_fraction"] > 0.9
        assert observation.value is not None, "a well-lit roof should classify"


@needs_cache
def test_real_cache_green_roof_negatives_are_strings_with_published_judgeability() -> None:
    """RTI-009 on the real imagery: negatives are "not_detected" (never Boolean False) and
    carry the vegetation judgeability that permitted them. vie-swv-010's judgeable share
    (0.68) sits below the general negative judgeability floor, so it abstains — by the same
    general threshold every building is measured against, not by any building exception."""
    floor = float(CFG.threshold("image", "green_roof", "negative_min_judgeable_fraction"))
    expected = {
        "vie-swv-001": "not_detected",
        "vie-swv-007": "not_detected",
        "vie-swv-008": "not_detected",
        "vie-swv-010": None,
    }
    for building_id, want in expected.items():
        observation = vegetation.observe_green_roof(cached_crop(building_id), CFG)
        assert observation.value == want, f"{building_id}: {observation.rationale}"
        assert not isinstance(observation.value, bool)
        judgeable = observation.quality["vegetation_judgeable_fraction"]
        if want == "not_detected":
            assert judgeable >= floor
        else:
            # Above the whole-roof abstention gate but below the negative floor: the exact
            # band the floor exists for, and the diagnostics stay published.
            assert 1.0 - ABSTAIN_ABOVE <= judgeable < floor
            assert observation.quality["vegetated_fraction"] is not None
            assert "judgeability floor" in observation.rationale


@needs_cache
def test_real_cache_roof_form_is_pitched_on_007_and_honestly_unknown_on_008() -> None:
    """RTI-005 on the real imagery. 008's imagery genuinely shows two strong brightness
    populations (bright upper roof against its darker lower deck), so the honest general
    outcome is None, not a forced "flat"; the published features carry the evidence."""
    pitched = roof_form.observe_roof_form(
        cached_crop("vie-swv-007"), CFG, _cached_geometry("vie-swv-007")
    )
    assert pitched.value == "pitched"

    unknown = roof_form.observe_roof_form(
        cached_crop("vie-swv-008"), CFG, _cached_geometry("vie-swv-008")
    )
    assert unknown.value != "pitched", "008 must not read pitched once its ridge is withheld"
    assert unknown.value is None  # the honest general result on this imagery
    assert unknown.quality["features"]["otsu_plane_contrast"] is not None
    assert unknown.quality["ridge_observation"]["value"] is None
