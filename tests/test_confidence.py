"""Confidence: the limits have to be structural, or they are decoration.

These tests are about what the module *refuses* to do. The arithmetic is one multiplication and
needs one test; the interesting properties are that a fourth penalty factor cannot be added,
that a factor cannot fall outside [0.5, 1.0], that no negative detection can be published above
its cap however it is called, and that an abstention never turns into a scored ``false``.

Where a test is about a threshold's behaviour rather than its value, it pins the threshold in a
copy of the config so the test states its own premise. Everything else reads the real
``configs/pipeline.yaml``, so a change of policy there shows up here.
"""

from __future__ import annotations

import itertools
from dataclasses import replace

import pytest

from propx_roofs import confidence, config
from propx_roofs.types import Attribute, Availability, Confidence, ImageObservation

IMAGERY = ("Orthofoto 2024 Wien",)


@pytest.fixture(scope="module")
def cfg() -> config.Config:
    return config.load()


def _cfg_with_confidence(original: config.Config, **overrides: object) -> config.Config:
    """A config whose ``confidence`` block is patched by the test, leaving the rest untouched."""
    thresholds = dict(original.thresholds)
    thresholds["confidence"] = {**thresholds["confidence"], **overrides}
    return replace(original, thresholds=thresholds)


def _observation(value: object, **quality: object) -> ImageObservation:
    return ImageObservation(
        name="solar_panels",
        value=value,
        method="rgb_cluster_detection",
        rationale="regular dark low-saturation cluster covering 34% of the roof interior",
        quality=dict(quality),
        limitations=("15 cm GSD with JPEG compression",),
    )


# --- 1. a null value needs an availability that explains it ---------------------------------


@pytest.mark.parametrize(
    "availability",
    [Availability.OBSERVED, Availability.DERIVED, Availability.INFERRED],
)
def test_null_value_with_a_positive_availability_is_refused(
    cfg: config.Config, availability: Availability
) -> None:
    """Missing data is never zero and never invented — types.py enforces it at construction."""
    conf = confidence.build(availability, "m", "r", IMAGERY, cfg=cfg)
    with pytest.raises(ValueError, match="null value requires availability"):
        Attribute(value=None, availability=availability, confidence=conf)


def test_builders_cannot_route_around_the_null_rule(cfg: config.Config) -> None:
    """The same invariant has to hold through ``from_image_observation``, in both directions."""
    abstained = confidence.from_image_observation(
        _observation(None), "solar_panels", availability=Availability.OBSERVED, cfg=cfg
    )
    # A requested availability of ``observed`` cannot survive an abstention: there is nothing
    # observed to attach it to, so the builder publishes ``unavailable`` instead.
    assert abstained.availability is Availability.UNAVAILABLE
    assert abstained.availability.permits_null()

    # And the mirror image: a determined value may not be filed as absent data.
    with pytest.raises(ValueError, match="reserved for absent data"):
        confidence.from_image_observation(
            _observation(True), "solar_panels", availability=Availability.UNAVAILABLE, cfg=cfg
        )


# --- 2. score arithmetic, with explicit numbers ----------------------------------------------


def test_base_only_score_is_the_configured_base(cfg: config.Config) -> None:
    assert confidence.score(Availability.AUTHORITATIVE, {}, cfg=cfg) == 0.95
    assert confidence.score("derived", {}, cfg=cfg) == 0.85
    assert confidence.score("observed", {}, cfg=cfg) == 0.80
    assert confidence.score("inferred", {}, cfg=cfg) == 0.65


def test_score_is_base_times_the_product_of_factors(cfg: config.Config) -> None:
    """The vie-swv-008 case from design section 8: 0.95 x 0.95 x 0.70 = 0.63175 -> 0.632."""
    assert confidence.score(
        "authoritative", {"source_recency": 0.95, "image_conflict": 0.70}, cfg=cfg
    ) == 0.632
    # 0.85 x 0.70 = 0.595, a derived value under heavy shadow.
    assert confidence.score("derived", {"shadow_heavy": 0.70}, cfg=cfg) == 0.595
    # Three factors multiply in full: 0.65 x 0.95 x 0.85 x 0.70 = 0.3674125 -> 0.367.
    assert confidence.score(
        "inferred",
        {"source_recency": 0.95, "join_ambiguous": 0.85, "shadow_heavy": 0.70},
        cfg=cfg,
    ) == 0.367


def test_score_is_clipped_to_one_even_if_a_config_asks_for_more(cfg: config.Config) -> None:
    """Clipping is not decorative: a mis-edited base must not produce a score above 1."""
    loud = _cfg_with_confidence(cfg, base={**cfg.threshold("confidence", "base"), "derived": 1.4})
    assert confidence.score("derived", {}, cfg=loud) == 1.0
    assert confidence.score("derived", {"source_recency": 0.95}, cfg=loud) == 1.0


def test_cap_is_applied_after_the_product(cfg: config.Config) -> None:
    roof_type_cap = confidence.positive_cap("roof_type_authoritative", cfg=cfg)
    assert roof_type_cap == 0.90

    # 0.95 alone would exceed the section 5 cap for an authoritative roof type.
    assert confidence.score("authoritative", {}, cap=roof_type_cap, cfg=cfg) == 0.90
    # A score already under the cap is left alone.
    assert confidence.score(
        "authoritative", {"source_recency": 0.95, "image_conflict": 0.70}, cap=roof_type_cap,
        cfg=cfg,
    ) == 0.632
    with pytest.raises(ValueError, match=r"cap 1.4 outside \[0, 1\]"):
        confidence.score("observed", {}, cap=1.4, cfg=cfg)


def test_score_needs_a_method_and_a_rationale(cfg: config.Config) -> None:
    """A bare number is a number a reader can only take on trust."""
    with pytest.raises(ValueError, match="requires a method"):
        confidence.build("observed", "  ", "detected a regular cluster", cfg=cfg)
    with pytest.raises(ValueError, match="requires a rationale"):
        confidence.build("observed", "rgb_cluster_detection", "", cfg=cfg)


def test_build_publishes_everything_needed_to_recompute_the_score(cfg: config.Config) -> None:
    conf = confidence.build(
        "authoritative",
        "authoritative_dachform",
        "authoritative roof form for this exact outline",
        ("PV-Potenzial 2025 (ogdwien:ANLAGENLEISTUNG2025OGD)",),
        {"source_recency": 0.95, "image_conflict": 0.70},
        ("binary flat/pitched; cannot express gable, hip or mansard",),
        confidence.positive_cap("roof_type_authoritative", cfg=cfg),
        cfg=cfg,
    )
    published = conf.as_dict()

    assert published["score"] == 0.632
    assert published["factors"] == {"source_recency": 0.95, "image_conflict": 0.70}
    assert published["method"] and published["rationale"]
    # The note is not optional and is never claimed to be a calibration.
    assert published["note"] == (
        "Documented heuristic reliability indicator, not a calibrated probability."
    )
    assert "calibrated" not in published["rationale"]


def test_configured_caps_resolve_and_an_unknown_attribute_still_fails_loudly(
    cfg: config.Config,
) -> None:
    """Every attribute in the design section 5 table must now resolve to a configured cap.

    The gap this test originally caught is closed: pipeline.yaml once capped only five
    attributes, so the rest published uncapped. It now covers the whole table, and the test
    asserts the two halves that matter — configured caps come back with their real values,
    and a key nobody configured still raises instead of defaulting to something invented.
    """
    assert confidence.positive_cap("roof_area_m2", cfg=cfg) == 0.85
    assert confidence.positive_cap("mean_slope_deg", cfg=cfg) == 0.90
    assert confidence.positive_cap("visual_surface_appearance", cfg=cfg) == 0.60

    with pytest.raises(KeyError, match=r"missing threshold confidence\.positive_cap\.no_such_attr"):
        confidence.positive_cap("no_such_attr", cfg=cfg)


# --- 3. at most three factors, each in [0.5, 1.0] --------------------------------------------


def test_a_fourth_factor_is_refused(cfg: config.Config) -> None:
    four = {
        "source_recency": 0.95,
        "image_conflict": 0.70,
        "join_ambiguous": 0.85,
        "shadow_heavy": 0.70,
    }
    with pytest.raises(ValueError, match="at most 3"):
        confidence.score("observed", four, cfg=cfg)
    with pytest.raises(ValueError, match="at most 3"):
        confidence.build("observed", "m", "r", IMAGERY, four, cfg=cfg)
    with pytest.raises(ValueError, match="at most 3"):
        confidence.negative_detection("m", "r", IMAGERY, four, cfg=cfg)
    with pytest.raises(ValueError, match="at most 3"):
        confidence.from_image_observation(
            _observation(True), "solar_panels", factors=four, cfg=cfg
        )


@pytest.mark.parametrize("value", [0.49, 0.0, -0.2, 1.01, 1.5, 2.0])
def test_a_factor_outside_the_permitted_range_is_refused(
    cfg: config.Config, value: float
) -> None:
    """Below 0.5 a single heuristic would more than halve a score; above 1.0 it would be a bonus."""
    with pytest.raises(ValueError, match=r"outside \[0.5, 1.0\]"):
        confidence.score("observed", {"shadow_heavy": value}, cfg=cfg)


@pytest.mark.parametrize("value", [True, "0.9", None])
def test_a_non_numeric_factor_is_refused(cfg: config.Config, value: object) -> None:
    with pytest.raises(ValueError, match="is not a number"):
        confidence.score("observed", {"shadow_heavy": value}, cfg=cfg)  # type: ignore[dict-item]


def test_exactly_three_factors_is_still_allowed(cfg: config.Config) -> None:
    """0.80 x 0.95 x 0.85 x 0.70 = 0.4522, published to three decimals as 0.452."""
    three = {"source_recency": 0.95, "join_ambiguous": 0.85, "shadow_heavy": 0.70}
    assert confidence.score("observed", three, cfg=cfg) == 0.452


def test_configured_penalties_all_satisfy_the_factor_rule(cfg: config.Config) -> None:
    """Every penalty in pipeline.yaml must be usable as a factor; otherwise the config is wrong."""
    for name in cfg.threshold("confidence", "penalty"):
        assert 0.5 <= confidence.penalty(name, cfg=cfg) <= 1.0


# --- 4. no negative detection can exceed the cap ---------------------------------------------


def test_no_negative_detection_can_exceed_the_cap(cfg: config.Config) -> None:
    """Swept over every base and every legal factor combination, plus a config with base 1.0.

    Section 6.1: absence of evidence is weak evidence of absence. The cap is the whole point of
    ``negative_detection``, which is why that function has no ``cap`` parameter to override.
    """
    cap = confidence.negative_detection_cap(cfg=cfg)
    assert cap == 0.45

    inflated = _cfg_with_confidence(
        cfg,
        base={key: 1.0 for key in cfg.threshold("confidence", "base")},
    )
    factor_values = (0.5, 0.7, 0.85, 0.95, 1.0)

    for candidate in (cfg, inflated):
        for base_key in ("authoritative", "derived", "observed", "inferred"):
            for n in range(0, confidence.MAX_FACTORS + 1):
                for combo in itertools.product(factor_values, repeat=n):
                    factors = {f"f{i}": v for i, v in enumerate(combo)}
                    conf = confidence.negative_detection(
                        "rgb_cluster_detection",
                        "no regular dark cluster detected",
                        IMAGERY,
                        factors,
                        base_key=base_key,
                        cfg=candidate,
                    )
                    assert conf.score is not None
                    assert conf.score <= cap, (base_key, factors, conf.score)


def test_negative_detection_says_absence_of_evidence_and_names_its_cap(
    cfg: config.Config,
) -> None:
    conf = confidence.negative_detection(
        "rgb_cluster_detection", "no regular dark cluster detected", IMAGERY, cfg=cfg
    )
    assert conf.score == 0.45
    assert "absence of evidence is weak evidence of absence" in conf.rationale
    assert any("capped at 0.45" in limitation for limitation in conf.limitations)


def test_negative_detection_does_not_repeat_the_clause(cfg: config.Config) -> None:
    said_it = "nothing detected; absence of evidence is weak evidence of absence"
    conf = confidence.negative_detection("rgb_cluster_detection", said_it, IMAGERY, cfg=cfg)
    assert conf.rationale.lower().count("absence of evidence") == 1


def test_a_false_image_observation_lands_on_the_negative_cap(cfg: config.Config) -> None:
    attribute = confidence.from_image_observation(
        _observation(False, shadow_fraction=0.05), "solar_panels", cfg=cfg
    )
    assert attribute.value is False
    assert attribute.availability is Availability.OBSERVED
    assert attribute.confidence.score == confidence.negative_detection_cap(cfg=cfg)
    # A negative is not silently stronger than a positive would have been.
    assert attribute.confidence.score < confidence.positive_cap("solar_panels", cfg=cfg)


def test_a_not_detected_string_observation_lands_on_the_negative_cap(
    cfg: config.Config,
) -> None:
    """RTI-009: detection observations carry three-state strings, and "not_detected" is the
    negative-detection path — same cap, same absence-of-evidence clause as Boolean False."""
    observation = ImageObservation(
        name="green_roof",
        value="not_detected",
        method="rgb_vegetation_index_exg",
        rationale="ExG below threshold over a judgeable roof",
        quality={"shadow_fraction": 0.02},
    )
    attribute = confidence.from_image_observation(observation, "green_roof", cfg=cfg)
    assert attribute.value == "not_detected"
    assert attribute.confidence.score == confidence.negative_detection_cap(cfg=cfg)
    assert "absence of evidence" in attribute.confidence.rationale

    detected = confidence.from_image_observation(
        ImageObservation(
            name="green_roof",
            value="detected",
            method="rgb_vegetation_index_exg",
            rationale="ExG above threshold on a third of the roof",
            quality={"shadow_fraction": 0.02},
        ),
        "green_roof",
        cfg=cfg,
    )
    # "detected" is a positive detection and must not be dragged onto the negative cap.
    assert detected.confidence.score == confidence.positive_cap("green_roof", cfg=cfg)


def test_negative_value_recognition_is_type_aware(cfg: config.Config) -> None:
    """0.0 == False in Python: a measured zero (azimuth, fraction) must not score negative."""
    assert confidence.is_negative_observation_value(False) is True
    assert confidence.is_negative_observation_value("not_detected") is True
    assert confidence.is_negative_observation_value(0.0) is False
    assert confidence.is_negative_observation_value(0) is False
    assert confidence.is_negative_observation_value("detected") is False
    assert confidence.is_negative_observation_value(None) is False

    zero_azimuth = confidence.from_image_observation(
        ImageObservation(
            name="ridge_orientation_deg",
            value=0.0,
            method="hough",
            rationale="a ridge bearing exactly north",
        ),
        "ridge_orientation",
        cfg=cfg,
    )
    assert zero_azimuth.value == 0.0
    assert zero_azimuth.confidence.score == confidence.positive_cap("ridge_orientation", cfg=cfg)


def test_a_true_image_observation_lands_on_the_positive_cap(cfg: config.Config) -> None:
    attribute = confidence.from_image_observation(
        _observation(True, shadow_fraction=0.05),
        "solar_panels",
        sources=IMAGERY,
        factors={"source_recency": 0.95},
        cfg=cfg,
    )
    # 0.80 x 0.95 = 0.76, above the 0.75 ceiling anchored by design section 1.6.
    assert attribute.confidence.score == confidence.positive_cap("solar_panels", cfg=cfg) == 0.75
    assert attribute.image_evidence == {
        "indicates": True,
        "method": "rgb_cluster_detection",
        "rationale": (
            "regular dark low-saturation cluster covering 34% of the roof interior"
        ),
        "quality": {"shadow_fraction": 0.05},
    }


# --- 5. abstention ---------------------------------------------------------------------------


def test_an_abstaining_observation_publishes_no_score(cfg: config.Config) -> None:
    """A shadowed roof yields ``unknown``, not ``false`` (section 6.1)."""
    attribute = confidence.from_image_observation(
        _observation(None, shadow_fraction=0.62),
        "solar_panels",
        sources=IMAGERY,
        factors={"shadow_heavy": 0.70},
        cfg=cfg,
    )

    assert attribute.value is None
    assert attribute.availability is Availability.UNAVAILABLE
    assert attribute.confidence.score is None
    # No penalty is recorded, because there is no score for a penalty to have been applied to.
    assert attribute.confidence.factors == {}
    assert any("abstention, not a negative finding" in x for x in attribute.confidence.limitations)
    assert attribute.image_evidence is not None
    assert attribute.image_evidence["quality"] == {"shadow_fraction": 0.62}

    published = attribute.as_dict()
    assert published["value"] is None
    assert published["availability"] == "unavailable"
    assert published["confidence"]["score"] is None


def test_a_confidence_without_a_score_still_carries_the_note() -> None:
    """Abstaining does not exempt a record from saying what its numbers are not."""
    conf = Confidence(method="rgb_cluster_detection", rationale="roof interior too dark to judge")
    assert conf.score is None
    assert conf.as_dict()["note"].endswith("not a calibrated probability.")


# --- evidence-sensitive factors (RTI-008) ----------------------------------------------------


def test_evidence_factor_maps_the_gate_to_the_strongest_discount(cfg: config.Config) -> None:
    """At the publishability gate (lo) the factor is 0.5 - the strongest discount the section 6
    rules allow - because evidence that only just cleared the gate is the weakest evidence
    permitted to publish at all."""
    anchors = cfg.threshold("confidence", "evidence_factor", "judgeable_fraction")
    assert confidence.evidence_factor("judgeable_fraction", anchors["lo"], cfg=cfg) == 0.5


def test_evidence_factor_maps_the_ideal_measurement_to_no_discount(cfg: config.Config) -> None:
    anchors = cfg.threshold("confidence", "evidence_factor", "judgeable_fraction")
    assert confidence.evidence_factor("judgeable_fraction", anchors["hi"], cfg=cfg) == 1.0


def test_evidence_factor_is_linear_between_the_anchors(cfg: config.Config) -> None:
    anchors = cfg.threshold("confidence", "evidence_factor", "surface_margin")
    lo, hi = float(anchors["lo"]), float(anchors["hi"])
    midpoint = (lo + hi) / 2.0
    assert confidence.evidence_factor("surface_margin", midpoint, cfg=cfg) == pytest.approx(
        0.75, abs=1e-4
    )


def test_evidence_factor_clips_outside_the_anchors(cfg: config.Config) -> None:
    """Measurements beyond the anchors clip: the factor can never leave [0.5, 1.0], so it can
    never violate the section 6 factor range however extreme the measurement."""
    assert confidence.evidence_factor("ridge_gate_margin", -5.0, cfg=cfg) == 0.5
    assert confidence.evidence_factor("ridge_gate_margin", 99.0, cfg=cfg) == 1.0


def test_evidence_factor_output_is_always_a_legal_penalty_factor(cfg: config.Config) -> None:
    for name in (
        "judgeable_fraction",
        "surface_margin",
        "surface_pair_share",
        "vegetation_judgeable_fraction",
        "vegetation_index_margin",
        "ridge_gate_margin",
    ):
        for measured in (-1.0, 0.0, 0.3, 0.65, 0.8, 1.0, 2.0):
            factor = confidence.evidence_factor(name, measured, cfg=cfg)
            confidence.validate_factors({name: factor})  # raises if outside [0.5, 1.0]


def test_evidence_factor_refuses_unknown_names(cfg: config.Config) -> None:
    """A factor nobody configured anchors for is a loud failure, not an invented mapping."""
    with pytest.raises(KeyError):
        confidence.evidence_factor("no_such_quantity", 0.5, cfg=cfg)


def test_evidence_factor_refuses_degenerate_anchors(cfg: config.Config) -> None:
    broken = _cfg_with_confidence(cfg, evidence_factor={"broken": {"lo": 0.5, "hi": 0.5}})
    with pytest.raises(ValueError, match="lo < hi"):
        confidence.evidence_factor("broken", 0.5, cfg=broken)
