"""Confidence scores: documented heuristics with a structural ceiling.

``score = base(provenance) x product(penalties)``, clipped to [0, 1] and then to the
attribute's cap (design section 6). Every number behind that formula lives in
``configs/pipeline.yaml``; nothing here carries a literal threshold, so a change of policy is
a config edit and shows up in ``config_hash``.

Three properties are enforced by construction rather than by review, because each of them is
the kind of thing that decays quietly:

* **At most three penalty factors, each in [0.5, 1.0]** (section 6). Without a hard limit,
  "one more small factor" is always defensible and a score ends up a product of eight
  unexplainable numbers. With the limit, a fourth factor is a design decision someone has to
  make deliberately.
* **A score cannot exist without a method and a rationale.** ``build`` refuses to produce a
  ``Confidence`` otherwise, so no reader ever meets a bare number.
* **Negative detections cannot exceed ``confidence.negative_detection_cap``** (section 6.1).
  ``negative_detection`` does not take a cap argument at all, so no caller can raise it. This
  matters because absence of evidence is weak evidence of absence: the city's own 7.5 cm
  pipeline recovers only ~70% of existing installations (section 1.6), and ours reads 15 cm
  JPEG tiles.

None of these scores is calibrated. They were not fitted to labelled ground truth and they are
not probabilities; ``Confidence.as_dict`` stamps that on every record it emits, and this module
deliberately does not repeat the sentence in ``limitations`` where it would read as a second,
independent claim.

**Image-derived attributes carry evidence-sensitive factors; authoritative attributes do not**
(RTI-008). :func:`evidence_factor` maps a measured per-building quality quantity (judgeable
fraction, dominance margin, gate margin, index distance-from-threshold) linearly onto a named
penalty factor, so scores co-vary with the published evidence instead of being one constant
across buildings whose evidence differs. Authoritative and enrichment attributes deliberately
stay category-based: their reliability derives from the *source* (an official cadastre read
verbatim), not from anything this pipeline measured about this building's pixels, so a
per-building factor there would be a number with no measurement behind it. The exceptions are
already-measured properties of the *join*, not the source — the typology overlap share and the
image-conflict penalty — which stay as they are.

Caps for attributes that are *not* image detections (``roof_area_m2``, ``mean_slope_deg``, …)
are listed in the design section 5 table but not all of them appear in ``configs/pipeline.yaml``.
``positive_cap`` therefore fails loudly on an unknown key instead of inventing a ceiling: the
missing caps belong in the config, not in a caller's literal.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import lru_cache
from math import prod

from .config import Config, load
from .types import Attribute, Availability, Confidence, ImageObservation

# Section 6: at most three penalties, each in [0.5, 1.0]. A factor below 0.5 would let a
# single heuristic more than halve a score, which is a stronger claim than any of our
# evidence supports; a factor above 1.0 would be a bonus, and nothing here earns confidence.
MAX_FACTORS = 3
FACTOR_MIN = 0.5
FACTOR_MAX = 1.0

# Scores are rounded so a reader can reproduce the product by hand from the published
# ``factors`` without meeting float noise (0.6317500000000001). Rounding is applied before the
# cap is re-checked, so it can never lift a score over a ceiling.
SCORE_DECIMALS = 3

_ABSENCE_CLAUSE = "absence of evidence is weak evidence of absence"

# Observation values that mean "looked and found nothing" (section 6.1). Explicit rather than
# `is False`, because detection observations now carry three-state string values
# ("detected" / "not_detected" / None) and a negative that stopped looking like `False` must
# not silently start scoring as a positive. `False` stays for any remaining Boolean detectors.
NEGATIVE_OBSERVATION_VALUES = (False, "not_detected")


def is_negative_observation_value(value: object) -> bool:
    """Whether an observation value means "looked and found nothing".

    Type-aware on purpose: ``0.0 == False`` is true in Python, so an equality test would score
    a measured 0.0 (a legal azimuth, a legal fraction) as a negative detection.
    """
    if isinstance(value, bool):
        return value is False
    return isinstance(value, str) and value in NEGATIVE_OBSERVATION_VALUES
_ABSTAIN_LIMITATION = (
    "no value was determined, so no score is published; this is an abstention, not a negative "
    "finding"
)


@lru_cache(maxsize=1)
def _default_config() -> Config:
    """The repo config, loaded once. Passing ``cfg=`` explicitly overrides it in tests."""
    return load()


def _resolve(cfg: Config | None) -> Config:
    return cfg if cfg is not None else _default_config()


def _base_name(base_key: Availability | str) -> str:
    return base_key.value if isinstance(base_key, Availability) else str(base_key)


def base(base_key: Availability | str, *, cfg: Config | None = None) -> float:
    """Provenance base from ``confidence.base``.

    ``unavailable`` and ``not_in_source`` have no base by design: there is no value to be
    confident about, so the lookup fails rather than returning a placeholder.
    """
    return float(_resolve(cfg).threshold("confidence", "base", _base_name(base_key)))


def penalty(name: str, *, cfg: Config | None = None) -> float:
    """One named penalty from ``confidence.penalty``, validated against the [0.5, 1.0] rule."""
    value = float(_resolve(cfg).threshold("confidence", "penalty", name))
    if not FACTOR_MIN <= value <= FACTOR_MAX:
        raise ValueError(
            f"configured penalty {name}={value} outside [{FACTOR_MIN}, {FACTOR_MAX}]; "
            f"the confidence model in design section 6 does not permit it"
        )
    return value


def positive_cap(attribute: str, *, cfg: Config | None = None) -> float:
    """Cap for a positive/authoritative attribute from ``confidence.positive_cap``.

    ``confidence.positive_cap`` covers every attribute in the design section 5 table. Unknown
    keys raise, because the honest response to a cap nobody configured is a loud failure
    rather than a literal invented at the call site.
    """
    return float(_resolve(cfg).threshold("confidence", "positive_cap", attribute))


def negative_detection_cap(*, cfg: Config | None = None) -> float:
    return float(_resolve(cfg).threshold("confidence", "negative_detection_cap"))


def evidence_factor(name: str, measured: float, *, cfg: Config | None = None) -> float:
    """An evidence-sensitive penalty factor from a measured quality quantity (RTI-008).

    Linear map of ``measured`` from the configured anchors
    ``confidence.evidence_factor.<name>.{lo, hi}`` onto [:data:`FACTOR_MIN`,
    :data:`FACTOR_MAX`], clipped at both ends::

        factor = 0.5 + 0.5 * clip((measured - lo) / (hi - lo), 0, 1)

    The anchors follow one principle (documented at each entry in ``configs/pipeline.yaml``):
    ``lo`` is the publishability gate for that quantity — evidence that only just clears the
    gate is the weakest evidence permitted to publish, so it earns the strongest discount the
    section 6 factor rules allow — and ``hi`` is the ideal measurement, which earns none. A
    linear map with published anchors is deliberately the simplest shape that makes the score
    co-vary with the evidence: any curve would imply a fitted relationship no ground truth
    supports. This is **not calibration** and the result is not a probability.

    The caller must ensure the measured input is published in the attribute's
    ``image_evidence.quality`` so a reader can reproduce the factor by hand.
    """
    anchors = _resolve(cfg).threshold("confidence", "evidence_factor", name)
    lo, hi = float(anchors["lo"]), float(anchors["hi"])
    if not hi > lo:
        raise ValueError(f"evidence_factor {name}: anchors lo={lo} hi={hi} must satisfy lo < hi")
    position = min(max((float(measured) - lo) / (hi - lo), 0.0), 1.0)
    return round(FACTOR_MIN + (FACTOR_MAX - FACTOR_MIN) * position, 4)


def validate_factors(factors: Mapping[str, float] | None) -> dict[str, float]:
    """Enforce the section 6 factor rules. Returns a plain copy so the caller cannot mutate it."""
    factors = dict(factors or {})
    if len(factors) > MAX_FACTORS:
        raise ValueError(
            f"{len(factors)} penalty factors ({', '.join(sorted(factors))}); the confidence "
            f"model permits at most {MAX_FACTORS} per attribute (design section 6)"
        )
    for name, value in factors.items():
        if not name or not isinstance(name, str):
            raise ValueError(f"penalty factors must be named, got {name!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"penalty factor {name}={value!r} is not a number")
        if not FACTOR_MIN <= float(value) <= FACTOR_MAX:
            raise ValueError(
                f"penalty factor {name}={value} outside [{FACTOR_MIN}, {FACTOR_MAX}] "
                f"(design section 6)"
            )
    return {name: float(value) for name, value in factors.items()}


def score(
    base_key: Availability | str,
    factors: Mapping[str, float] | None = None,
    cap: float | None = None,
    *,
    cfg: Config | None = None,
) -> float:
    """``base(provenance) x product(penalties)``, clipped to [0, 1] and then to ``cap``."""
    if cap is not None and not 0.0 <= cap <= 1.0:
        raise ValueError(f"cap {cap} outside [0, 1]")
    raw = base(base_key, cfg=cfg) * prod(validate_factors(factors).values())
    clipped = min(max(raw, 0.0), 1.0)
    rounded = round(clipped, SCORE_DECIMALS)
    return min(rounded, cap) if cap is not None else rounded


def build(
    base_key: Availability | str,
    method: str,
    rationale: str,
    sources: Iterable[str] = (),
    factors: Mapping[str, float] | None = None,
    limitations: Iterable[str] = (),
    cap: float | None = None,
    *,
    cfg: Config | None = None,
) -> Confidence:
    """A ``Confidence`` whose score is reproducible from its own published fields.

    ``method`` and ``rationale`` are mandatory: a score with neither is a number a reader can
    only take on trust, which is the failure mode this whole design is built against.
    """
    if not method or not method.strip():
        raise ValueError("a confidence score requires a method")
    if not rationale or not rationale.strip():
        raise ValueError("a confidence score requires a rationale")
    return Confidence(
        method=method,
        rationale=rationale,
        sources=tuple(sources),
        score=score(base_key, factors, cap, cfg=cfg),
        limitations=tuple(limitations),
        factors=validate_factors(factors),
    )


def negative_detection(
    method: str,
    rationale: str,
    sources: Iterable[str] = (),
    factors: Mapping[str, float] | None = None,
    limitations: Iterable[str] = (),
    base_key: Availability | str = Availability.OBSERVED,
    *,
    cfg: Config | None = None,
) -> Confidence:
    """A "nothing detected" confidence, capped by ``confidence.negative_detection_cap``.

    There is deliberately no ``cap`` parameter: the ceiling is the point of the function
    (section 6.1), and an overridable ceiling is not a ceiling. The rationale is extended with
    the absence-of-evidence clause when the caller has not already said it, so no negative
    detection is published as though it were a measurement of absence.
    """
    cap = negative_detection_cap(cfg=cfg)
    if _ABSENCE_CLAUSE not in rationale.lower():
        rationale = f"{rationale.rstrip().rstrip('.')}; {_ABSENCE_CLAUSE}"
    return build(
        base_key,
        method,
        rationale,
        sources,
        factors,
        (*limitations, f"negative detections are capped at {cap} (design section 6.1)"),
        cap,
        cfg=cfg,
    )


def from_image_observation(
    obs: ImageObservation,
    positive_cap_key: str,
    *,
    availability: Availability = Availability.OBSERVED,
    unit: str | None = None,
    sources: Iterable[str] = (),
    factors: Mapping[str, float] | None = None,
    limitations: Iterable[str] = (),
    cfg: Config | None = None,
) -> Attribute:
    """Publish an ``ImageObservation`` as an ``Attribute``, applying the section 6.1 asymmetry.

    * ``obs.value is None`` — the image stage abstained. ``UNAVAILABLE`` with **no score**:
      an abstention is not a negative finding and must not be scored as one.
    * ``obs.value`` in ``NEGATIVE_OBSERVATION_VALUES`` (``"not_detected"``, or ``False`` for
      Boolean detectors) — nothing detected on a readable roof. Capped at
      ``negative_detection_cap``.
    * anything else — a positive detection, capped at
      ``confidence.positive_cap.<positive_cap_key>``.

    ``availability`` applies to the determined-value paths only; an abstention is always
    ``UNAVAILABLE``, because the alternative is a null value wearing an availability that
    claims we observed something.
    """
    evidence = {
        "indicates": obs.value,
        "method": obs.method,
        "rationale": obs.rationale,
        "quality": dict(obs.quality),
    }
    shared = (*obs.limitations, *limitations)

    if obs.value is None:
        # Availability.UNAVAILABLE, score None: nothing to be confident about. Factors are
        # dropped rather than recorded, because a multiplier on a score that does not exist
        # would read as arithmetic that happened.
        return Attribute(
            value=None,
            availability=Availability.UNAVAILABLE,
            confidence=Confidence(
                method=obs.method,
                rationale=obs.rationale,
                sources=tuple(sources),
                score=None,
                limitations=(*shared, _ABSTAIN_LIMITATION),
                factors={},
            ),
            unit=unit,
            image_evidence=evidence,
        )

    if availability.permits_null():
        raise ValueError(
            f"observation {obs.name!r} determined the value {obs.value!r}, so availability "
            f"{availability.value} is wrong; that value is reserved for absent data"
        )

    if is_negative_observation_value(obs.value):
        confidence = negative_detection(
            obs.method,
            obs.rationale,
            sources,
            factors,
            shared,
            availability,
            cfg=cfg,
        )
    else:
        cap = positive_cap(positive_cap_key, cfg=cfg)
        confidence = build(
            availability,
            obs.method,
            obs.rationale,
            sources,
            factors,
            (*shared, f"confidence for {positive_cap_key} is capped at {cap}"),
            cap,
            cfg=cfg,
        )

    return Attribute(
        value=obs.value,
        availability=availability,
        confidence=confidence,
        unit=unit,
        image_evidence=evidence,
    )
