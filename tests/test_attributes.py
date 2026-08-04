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


def test_surface_class_names_a_visible_class_not_a_material() -> None:
    crop = synthetic_crop(grainy((190, 190, 195), (200, 200), spread=2))
    observation = surface.observe_surface_class(crop, CFG)
    assert isinstance(observation, ImageObservation)
    assert observation.value == "metal"  # smooth, bright, near-neutral
    assert "never a claim about construction material" in observation.rationale
    assert any("cannot establish construction material" in lim for lim in observation.limitations)


def test_surface_class_detects_vegetation_and_tile_signatures() -> None:
    green = synthetic_crop(grainy((70, 140, 60), (200, 200)))
    assert surface.observe_surface_class(green, CFG).value == "vegetated"

    # Warm, saturated and rough: fired clay with visible courses.
    tiled = grainy((165, 105, 80), (200, 200), spread=3)
    tiled[::6, :] = (110, 70, 55)  # tile courses every ~0.6 m
    assert surface.observe_surface_class(synthetic_crop(tiled), CFG).value == "tiled"


def test_surface_class_reports_mixed_and_abstains_without_pixels() -> None:
    half = np.zeros((200, 200, 3), np.uint8)
    half[:, :100] = grainy((165, 105, 80), (200, 100), spread=3)[:, :100]
    half[::6, :100] = (110, 70, 55)
    rough = grainy((150, 150, 152), (200, 100), spread=3)
    rough[:, ::4] = (95, 95, 97)  # gravel speckle
    half[:, 100:] = rough
    assert surface.observe_surface_class(synthetic_crop(half), CFG).value in {"mixed", "tiled"}

    empty = synthetic_crop(grainy((180, 180, 180), (50, 50)), mask=np.zeros((50, 50), bool))
    observation = surface.observe_surface_class(empty, CFG)
    assert observation.value is None
    assert "no judgeable roof pixels" in observation.rationale


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
    assert observation.value is True
    assert observation.quality["vegetated_fraction"] > CFG.threshold(
        "image", "green_roof", "min_coverage_fraction"
    )


def test_green_roof_reports_false_on_dormant_reddish_brown_substrate() -> None:
    """The expected outcome on a March/April sedum roof — and it must be stated, not hidden."""
    crop = synthetic_crop(grainy((150, 120, 105), (300, 300), spread=4))
    observation = vegetation.observe_green_roof(crop, CFG)
    assert observation.value is False
    assert observation.quality["vegetated_fraction"] < 0.01
    assert "dormant March/April vegetation" in observation.rationale
    assert any("DORMANT" in lim for lim in observation.limitations)


def test_green_roof_abstains_under_heavy_shadow() -> None:
    dark = synthetic_crop(grainy((32, 30, 32), (300, 300), spread=4))
    observation = vegetation.observe_green_roof(dark, CFG)
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
        surface.observe_surface_class(crop, CFG),
        orientation.observe_ridge_orientation(crop, CFG),
        orientation.observe_footprint_axis(square_wgs84(15.0), CFG),
        solar.observe_solar_panels(crop, CFG),
        vegetation.observe_green_roof(crop, CFG),
        vegetation.observe_vegetated_fraction(crop, CFG),
    ):
        assert isinstance(observation, ImageObservation)
        assert set(observation.as_dict()) == {
            "value",
            "method",
            "rationale",
            "quality",
            "limitations",
        }
