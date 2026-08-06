"""The offline, cache-backed run: cached sources in, ``roof_attributes.json`` out.

This module is **wiring**. Every measurement, threshold and judgement already lives in a
module built for it, and nothing here re-implements one. What it does own is the order of the
stages and the rules that only make sense between them:

* **The cache is verified before any building is touched** (:func:`verify_cache`, which calls
  ``tools/build_cache.verify`` rather than repeating it). A run against a cache with a
  truncated tile in it would publish agreement numbers describing our download.
* **Every published attribute value in metres comes from
  ``geometry.to_metric(..., EPSG:31256)`` only** — reached through ``geometry``, ``join`` and
  ``segment.agreement``, none of which can return a metric value any other way, and the
  reconnaissance cos(latitude) frame is never called here. Some *image diagnostics* under
  ``image_evidence.quality`` are necessarily measured on the pixel grid instead
  (``roof_mask_area_m2_approx``, solar ``clusters[].area_m2_approx``,
  ``longest_supporting_line_m_approx``). Each carries an ``_approx`` suffix and a
  ``measurement_frame`` / ``source_crs`` / ``note`` triple, and none feeds a published value.
* **The authoritative outline is always primary.** ``cv_candidate_polygon`` is written to its
  own field, and there is no branch anywhere below in which a CV polygon becomes
  ``authoritative_roof_polygon``. A segmentation failure sets that field to ``null`` and
  records ``failure_reason``: a reportable outcome, never an exception that aborts the run.
* **Nothing is read from the selection metadata in ``configs/study_area.yaml``.** That file
  pins ``building_id``, ``objectid`` and ``bw_geb_id`` and records why each building was
  chosen. Every published value — ``DACHFORM``, ``SLOPE_MEAN``, the PV figures, the address,
  the areas — is read live from ``data/cache/<area>/``. The pinned ``bw_geb_id`` is not even
  used to join; the join is recomputed and the config value is only a sanity comparison.
* **Absent data is ``not_in_source`` with a null value.** Never zero, never carried over from
  a neighbouring record, never inferred from the construction epoch.

The output is validated against the schema **shipped inside the package** before the caller is
told anything succeeded.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from shapely.geometry import shape

from . import confidence, config, geometry, imaging, join, provenance, schema, units
from .attributes import orientation, solar, surface, vegetation
from .config import Config
from .segment import agreement, grabcut
from .sources import wfs
from .types import (
    Attribute,
    Availability,
    Confidence,
    CvDelineation,
    ImageCrop,
    ImageObservation,
    JoinEvidence,
    RoofRecord,
)

logger = logging.getLogger(__name__)

ROOF_FILE = "roof_records_2025.geojson"
TYPOLOGY_FILE = "typology.geojson"
BUILDING_INFO_FILE = "building_info.geojson"

PRIMARY_POLYGON = "authoritative_roof_polygon"
ROOF_POLYGON_SOURCE = wfs.LAYER_ROOF_RECORD_2025
WGS84 = "EPSG:4326"

SCHEMA_PACKAGE = "propx_roofs.schema"
SCHEMA_RESOURCE = "roof_attributes.schema.json"

# Design section 5.1: matched on the exact observed string. "Schraegdach" is ASCII in this
# layer, and anything unrecognised maps to unknown rather than being coerced into a class.
DACHFORM_TO_ROOF_TYPE = {"Flachdach": "flat", "Schraegdach": "pitched"}

SUITABILITY_FIELDS = ("SCHLECHT", "MITTEL", "GUT", "SEHRGUT")
SUITABILITY_LABELS = {
    "SCHLECHT": "poor",
    "MITTEL": "medium",
    "GUT": "good",
    "SEHRGUT": "very_good",
}

# GEBAEUDETYPOGD carries the epoch in two columns and populates neither consistently.
# OBJ_STR_TXT is the field named in the design (section 1.1) and is the one published;
# OBJ_STR2_TXT is carried verbatim in source_detail so a reader can see what the other column
# said without the pipeline inventing a precedence rule the design does not define.
TYPOLOGY_EPOCH_FIELD = "OBJ_STR_TXT"
# GEBAEUDETYPOGD writes this exact string when it holds no epoch for a building. It is the
# source declining to answer, so publishing it as an authoritative VALUE would assert 0.9
# confidence in a non-answer and a reader could mistake it for a real epoch. Treated as
# absent (design section 5: "absent is availability not_in_source"), with the raw string kept
# in source_detail so nothing is lost and the placeholder stays traceable.
TYPOLOGY_NO_VALUE_PLACEHOLDER = "Keine Angabe"
TYPOLOGY_EPOCH_SECONDARY = "OBJ_STR2_TXT"
TYPOLOGY_TYPE_FIELD = "BAUTYP_TXT"


class PipelineError(RuntimeError):
    """The run cannot honestly continue. Always fatal; never downgraded to a warning."""


# --------------------------------------------------------------------------------------
# stage 1 - configuration and cache verification
# --------------------------------------------------------------------------------------


def load_config(
    study_area_path: Path | str | None = None, pipeline_path: Path | str | None = None
) -> Config:
    """Both config files, with the metric-CRS invariant already enforced by ``config.load``."""
    kwargs: dict[str, Any] = {}
    if study_area_path is not None:
        kwargs["study_area_path"] = study_area_path
    if pipeline_path is not None:
        kwargs["pipeline_path"] = pipeline_path
    return config.load(**kwargs)


def _build_cache_tool() -> Any:
    """Import ``tools/build_cache.py`` by path.

    The verifier is reused rather than reimplemented: a second copy of "is this cache fit to
    run" would drift from the one that built the cache, and the drift would show up as a run
    that passes its own check and fails the tool's.
    """
    path = config.REPO_ROOT / "tools" / "build_cache.py"
    if not path.exists():
        raise PipelineError(f"cache verifier not found at {path}")
    spec = importlib.util.spec_from_file_location("propx_build_cache", path)
    if spec is None or spec.loader is None:
        raise PipelineError(f"cannot load the cache verifier from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("propx_build_cache", module)
    spec.loader.exec_module(module)
    return module


def verify_cache(cfg: Config) -> list[str]:
    """Problems found in the committed cache; empty means fit to run. Offline."""
    return list(_build_cache_tool().verify(cfg))


def require_verified_cache(cfg: Config) -> None:
    """Refuse to process anything against a cache that failed verification."""
    problems = verify_cache(cfg)
    # The verifier prints its report to stdout while everything else logs to stderr; without a
    # flush the two interleave and the cache report appears to arrive after the buildings.
    sys.stdout.flush()
    if problems:
        raise PipelineError(
            "cache verification failed, so no building was processed:\n  "
            + "\n  ".join(problems)
            + "\nRebuild it with `python3 tools/build_cache.py`, which fetches from the live "
            "services. The pipeline itself never does."
        )
    logger.info("cache verified: %s", cfg.study_area.cache_dir)


# --------------------------------------------------------------------------------------
# stage 2 - authoritative roof records
# --------------------------------------------------------------------------------------


def _read_features(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise PipelineError(f"missing cached layer {path}; run `python3 tools/build_cache.py`")
    return list(json.loads(path.read_text(encoding="utf-8")).get("features", []))


def _roof_record(feature: dict[str, Any]) -> RoofRecord:
    """One cached ``ANLAGENLEISTUNG2025OGD`` feature, source field names preserved verbatim."""
    props = dict(feature.get("properties") or {})
    return RoofRecord(
        objectid=int(props["OBJECTID"]),
        geometry=feature["geometry"],
        dachform=props.get("DACHFORM"),
        slope_mean=props.get("SLOPE_MEAN"),
        anlagenleistung=props.get("ANLAGENLEISTUNG"),
        pot_ertrag_y_min=props.get("POT_ERTRAG_Y_MIN"),
        pot_ertrag_y_max=props.get("POT_ERTRAG_Y_MAX"),
        suitability_m2={field: props.get(field) for field in SUITABILITY_FIELDS},
        adresse=props.get("ADRESSE"),
        raw_properties=props,
    )


def load_roof_records(cfg: Config) -> dict[int, RoofRecord]:
    """Every cached 2025 roof record for the study bbox, keyed by ``OBJECTID``.

    All of them, not only the pinned ten: :func:`join.assign_cardinality` can only see that one
    building unit carries several roof records if it is shown every record that might claim it.
    Restricting the join to the sample would report ``1:1`` for a genuine ``1:n``.
    """
    features = _read_features(cfg.study_area.cache_dir / ROOF_FILE)
    records = {}
    for feature in features:
        if not feature.get("geometry"):
            continue
        record = _roof_record(feature)
        records[record.objectid] = record
    return records


def select_pinned(records: dict[int, RoofRecord], cfg: Config) -> dict[str, RoofRecord]:
    """The pinned records by ``building_id``, in config order. Missing ids are fatal.

    A silently dropped building would publish nine records where ten were promised, and the
    only visible symptom would be a smaller file.
    """
    missing = [b.objectid for b in cfg.study_area.buildings if b.objectid not in records]
    if missing:
        raise PipelineError(
            f"pinned roof record(s) {sorted(missing)} are not in "
            f"{cfg.study_area.cache_dir / ROOF_FILE}. The cache and configs/study_area.yaml "
            f"disagree about the sample; refusing to publish a partial one."
        )
    return {b.building_id: records[b.objectid] for b in cfg.study_area.buildings}


# --------------------------------------------------------------------------------------
# stage 3 - FMZK building units and the cross-check join
# --------------------------------------------------------------------------------------


def fmzk_ids_by_unit(cfg: Config) -> dict[int, list[int]]:
    """``BW_GEB_ID`` to the ``FMZK_ID``s dissolved into it, so the join is traceable to parts."""
    grouped: dict[int, list[int]] = {}
    for feature in units.load_parts(cfg):
        props = feature.get("properties") or {}
        unit_id, part_id = props.get(units.UNIT_ID_FIELD), props.get("FMZK_ID")
        if unit_id is None or part_id is None:
            continue
        grouped.setdefault(int(unit_id), []).append(int(part_id))
    return {key: sorted(value) for key, value in grouped.items()}


def join_units(
    records: dict[int, RoofRecord], cfg: Config
) -> tuple[dict[int, JoinEvidence], dict[int, list[int]]]:
    """Best-overlap match of every roof record against the buffered, dissolved FMZK units."""
    unit_map, null_id_parts, fetch_bbox = units.load_units(cfg)
    logger.info(
        "FMZK: %d units from the bbox buffered by %g m %s; %d part(s) carry no %s",
        len(unit_map),
        float(cfg.threshold("join", "fmzk_buffer_m")),
        tuple(round(v, 6) for v in fetch_bbox),
        null_id_parts,
        units.UNIT_ID_FIELD,
    )
    evidence = {
        objectid: join.match_roof_record(
            record.geometry, unit_map, cfg, fmzk_parts_null_id_in_area=null_id_parts
        )
        for objectid, record in records.items()
    }
    return join.assign_cardinality(evidence), fmzk_ids_by_unit(cfg)


# --------------------------------------------------------------------------------------
# stage 4 - imagery, segmentation and agreement
# --------------------------------------------------------------------------------------


def segment_roof(crop: ImageCrop, cfg: Config) -> tuple[dict[str, Any] | None, str | None]:
    """GrabCut candidate polygon for one crop, or ``(None, failure_reason)``.

    Design section 4 makes a null candidate a reportable outcome, so this returns a reason
    instead of raising and the building is published either way. An unexpected exception from
    OpenCV is caught for the same reason: one degenerate roof must not cost the other nine.
    """
    try:
        _, meta = grabcut.segment(crop, cfg)
    except Exception as error:  # noqa: BLE001 - a CV failure is data, not a crash
        logger.warning("segmentation raised %s: %s", type(error).__name__, error)
        return None, f"segmenter_raised_{type(error).__name__}"
    return meta.get("polygon"), meta.get("failure_reason")


def delineate(crop: ImageCrop, record: RoofRecord, cfg: Config) -> CvDelineation:
    """Candidate outline plus its agreement with the authoritative outline (never accuracy)."""
    polygon, failure_reason = segment_roof(crop, cfg)
    return agreement.compare(polygon, record.geometry, crop, cfg, failure_reason)


# --------------------------------------------------------------------------------------
# stage 5 - image observations
# --------------------------------------------------------------------------------------


def observe(crop: ImageCrop, record: RoofRecord, cfg: Config) -> dict[str, ImageObservation]:
    """Every image-derived observation for one roof, each still an abstention-capable value."""
    ridge, axis = orientation.observe(crop, record.geometry, cfg)
    return {
        "roof_surface_class": surface.observe_surface_class(crop, cfg),
        "solar_panels": solar.observe_solar_panels(crop, cfg),
        "green_roof": vegetation.observe_green_roof(crop, cfg),
        # No standalone vegetated_fraction observation: the measurement is already published as
        # green_roof's image_evidence.quality.vegetated_fraction, and calling
        # vegetation.observe_vegetated_fraction here only ran the shared index pass a second
        # time per building to produce a value nothing below reads.
        "ridge_orientation_deg": ridge,
        "footprint_axis_orientation_deg": axis,
    }


def image_roof_type(ridge: ImageObservation) -> tuple[str | None, str]:
    """What the imagery says about flat-versus-pitched, and why. ``None`` means *abstained*.

    A detected ridge — Hough segments that agree *and* a brightness difference across the
    strongest of them — indicates two differently-facing planes, so it indicates ``pitched``.

    **No ridge detected indicates nothing.** Mapping absence to ``flat`` would be the exact
    mistake design section 5.1 warns against: it would fire the conflict flag on every pitched
    roof whose ridge is too short, too shadowed or too dormer-broken to detect, and three of
    the ten selected buildings are in that position. ``provenance.build_conflict`` treats a
    ``None`` image value as agreement-by-abstention rather than a disagreement, which is why
    this function returns ``None`` rather than a guess.
    """
    if ridge.value is not None:
        return "pitched", (
            f"a ridge line was detected at {ridge.value} deg, which indicates two "
            f"differently-facing roof planes. {ridge.rationale}"
        )
    return None, (
        f"no flat-or-pitched reading is offered from the imagery: {ridge.rationale}. Absence "
        f"of a detected ridge is not evidence of a flat roof, so no image class is asserted "
        f"and no conflict with the authoritative value is claimed."
    )


# --------------------------------------------------------------------------------------
# stage 6 - authoritative enrichment by spatial join
# --------------------------------------------------------------------------------------


def _best_overlapping(
    roof_geom_wgs84: Any, features: list[dict[str, Any]], cfg: Config
) -> tuple[dict[str, Any] | None, float | None]:
    """The feature covering most of the roof outline, with that share, in EPSG:31256.

    Best overlap, not centroid containment: typology polygons are cut on a different cadastre
    and a roof centroid can land in a neighbouring parcel. There is deliberately **no minimum
    overlap threshold** — inventing one would be a decision the design does not make — so the
    measured share travels into ``source_detail`` and a reader can judge a weak match.
    """
    crs = str(cfg.threshold("crs", "metric"))
    roof_wgs84 = geometry.as_shapely(roof_geom_wgs84)
    roof_metric, roof_measure = geometry.to_metric(roof_wgs84, crs)
    if roof_measure.area_m2 <= 0:
        return None, None
    best: tuple[dict[str, Any], float] | None = None
    for feature in features:
        if not feature.get("geometry"):
            continue
        other = shape(feature["geometry"])
        if not roof_wgs84.intersects(other):
            continue
        other_metric, _ = geometry.to_metric(other, crs)
        share = roof_metric.intersection(other_metric).area / roof_measure.area_m2
        if share <= 0:
            continue
        if best is None or share > best[1]:
            best = (feature, share)
    return (None, None) if best is None else (dict(best[0].get("properties") or {}), best[1])


def _contained_point(
    roof_geom_wgs84: Any, features: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The building-info point inside the roof outline. Containment is exact for a point.

    Several points inside one outline would mean several addressed buildings under one roof
    record; the lowest ``OBJECTID`` is taken rather than merging attributes from two buildings
    into one. The count is **not** reported anywhere — no caller surfaces it, and on this sample
    every roof outline contains at most one point, so a multi-point record has never been
    produced. Publishing the count would need a schema field and a reviewer-facing meaning for
    it, which is out of scope here.
    """
    roof = geometry.as_shapely(roof_geom_wgs84)
    inside = [
        dict(f.get("properties") or {})
        for f in features
        if f.get("geometry") and roof.contains(shape(f["geometry"]))
    ]
    if not inside:
        return None
    return sorted(inside, key=lambda p: p.get("OBJECTID") or 0)[0]


# --------------------------------------------------------------------------------------
# stage 7 - attributes, confidence and provenance
# --------------------------------------------------------------------------------------


def source_names(cfg: Config) -> dict[str, str]:
    """Layer identifier to the citable source name published in ``run.sources``."""
    names = {}
    for descriptor in provenance.source_descriptors(cfg):
        names[descriptor.layer or descriptor.name] = descriptor.name
    return names


def _absent(
    layer: str,
    field: str,
    reason: str,
    source: str,
    extra_source_detail: dict[str, Any] | None = None,
) -> Attribute:
    """A value the source genuinely does not carry. Null, never zero, never inferred.

    ``extra_source_detail`` keeps the raw source content visible when the source *did* return
    something that does not amount to a value — e.g. GEBAEUDETYPOGD's ``"Keine Angabe"``
    placeholder. The value is absent; the bytes behind that judgement stay auditable.
    """
    return Attribute(
        value=None,
        availability=Availability.NOT_IN_SOURCE,
        confidence=Confidence(
            method="source_consulted_no_value",
            rationale=reason,
            sources=(source,),
            score=None,
            limitations=(
                "the source was consulted and carries no value here; null is the finding, not "
                "a placeholder for zero",
            ),
        ),
        source_detail={"layer": layer, "field": field, **(extra_source_detail or {})},
    )


def _withheld_solar_candidate(obs: ImageObservation, source: str) -> Attribute:
    """Publish an evaluated detector's raw candidate evidence without publishing a verdict.

    The RGB solar detector was spot-checked against the imagery on the ten selected buildings
    and is **anti-correlated** with what a human sees: the two roofs that unambiguously carry
    module arrays (vie-swv-001, vie-swv-003) come out negative, while two roofs with no
    defensible modules (vie-swv-006 — an extensive-substrate roof; vie-swv-010 — modules on the
    *neighbouring* building) come out positive. That is four errors in four checkable cases.

    A detector in that state has no business emitting a Boolean at 0.75 confidence, and the
    honest repair is *not* to move the gates until this particular sample agrees — that would
    be fitting the instrument to ten eyeballed guesses and calling the result a detection.

    A later evaluation used 35 held-out reference-negative roofs from a non-ground-truth
    reference made by two readings of the same imagery. The detector produced one false positive
    and failed its pre-registered zero-false-positive bar. The sample has only two positives in
    total, so it supports neither recall nor a general accuracy claim. This completed evaluation
    strengthens the abstention but does not turn the image readings into ground truth.

    So ``value`` is ``None`` with no score, and every raw diagnostic the detector produced is
    kept under ``image_evidence.quality`` — clearly labelled as withheld candidate evidence —
    because those numbers are exactly what an independent validation would need. Nothing is
    deleted and no threshold moves.
    """
    raw = obs.as_dict()
    quality = dict(raw.get("quality") or {})
    quality["status"] = "candidate_evidence_withheld_after_failed_evaluation"
    quality["interpretation"] = (
        "Raw candidate diagnostics from a detector that failed a completed evaluation against "
        "a non-ground-truth image-reading reference. NOT a Boolean observation and not evidence "
        "that panels are present or absent."
    )
    quality["withheld_detector_verdict"] = obs.value
    quality["detector_limitations"] = list(raw.get("limitations") or ())
    evidence = {
        # `indicates` is null on purpose: the image evidence supports no publishable finding.
        "indicates": None,
        "method": raw["method"],
        "rationale": (
            "candidate generation only; the detector's verdict is withheld after it failed a "
            "completed non-ground-truth reference evaluation, so this evidence indicates "
            "nothing about panel presence or absence"
        ),
        "quality": quality,
    }
    return Attribute(
        value=None,
        availability=Availability.UNAVAILABLE,
        confidence=Confidence(
            method="detector_withheld_after_failed_evaluation",
            rationale=(
                "the RGB detector is retained as a candidate generator only; it produced 1 "
                "false positive on 35 held-out reference-negative roofs and failed the "
                "pre-registered bar of zero, so no Boolean is published and no score is assigned"
            ),
            sources=(source,),
            score=None,
            limitations=(
                "the evaluation reference consists of readings of the same imagery, not "
                "independent ground truth, so it supports no general accuracy claim",
                "the reference sample contains only two positives in total, so it supports no "
                "recall estimate",
                "no gate was moved after the spot check to change any building's verdict",
                "candidate diagnostics are published for independent future validation, not as "
                "a finding",
            ),
        ),
        image_evidence=evidence,
    )


def _authoritative(
    value: Any,
    *,
    cap_key: str,
    method: str,
    rationale: str,
    source: str,
    source_detail: dict[str, Any],
    limitations: tuple[str, ...] = (),
    extra_factors: dict[str, float] | None = None,
    unit: str | None = None,
    cfg: Config,
) -> Attribute:
    """One authoritative attribute with the source-recency penalty applied and named."""
    factors = {"source_recency": confidence.penalty("source_recency", cfg=cfg)}
    factors.update(extra_factors or {})
    return Attribute(
        value=value,
        availability=Availability.AUTHORITATIVE,
        unit=unit,
        source_detail=source_detail,
        confidence=confidence.build(
            Availability.AUTHORITATIVE,
            method,
            rationale,
            (source,),
            factors,
            limitations,
            confidence.positive_cap(cap_key, cfg=cfg),
            cfg=cfg,
        ),
    )


def _image_factors(obs: ImageObservation, cfg: Config) -> dict[str, float]:
    """The one penalty an image observation can earn: a heavily shadowed roof interior.

    Applied only to determined values. An abstention carries no score at all, so a penalty on
    it would be arithmetic on a number that does not exist.
    """
    fraction = obs.quality.get("shadow_fraction")
    limit = float(cfg.threshold("image", "shadow_fraction_abstain"))
    if obs.value is None or fraction is None or float(fraction) <= limit:
        return {}
    return {"shadow_heavy": confidence.penalty("shadow_heavy", cfg=cfg)}


def _typology_overlap_factor(overlap_share: float, cfg: Config) -> float:
    """Transparent overlap reliability factor, not a calibrated probability.

    Best-overlap enrichment remains useful below complete containment, but treating a polygon
    covering 45% of a roof like one covering 100% made the published confidence contradict its
    own evidence. The measured share therefore travels directly into the score, clipped only to
    the confidence model's configured lower bound and to 1.0.
    """
    floor = confidence.penalty("typology_overlap_floor", cfg=cfg)
    return round(max(floor, min(1.0, float(overlap_share))), 4)


def build_roof_type(
    record: RoofRecord, ridge: ImageObservation, cfg: Config, sources: dict[str, str]
) -> Attribute:
    """``roof_type`` from ``DACHFORM``, with the image reading and any conflict kept separate.

    Three things stay independently visible and are never resolved into one number
    (design section 5.1): the authoritative class unmodified, what the imagery indicates as its
    own observation, and a conflict flag when the two disagree. The image value is never
    promoted and the authoritative value is never corrected.

    ``complex`` is not reachable: the design permits it only on additional evidence of multiple
    inconsistent ridge directions or multi-modal plane brightness, and no such detector is
    implemented. Emitting it from ``DACHFORM``, which is binary, would be an invented class.
    """
    source = sources[ROOF_POLYGON_SOURCE]
    raw = record.dachform
    value = DACHFORM_TO_ROOF_TYPE.get(raw, "unknown") if raw is not None else "unknown"
    indicates, rationale = image_roof_type(ridge)
    conflict = provenance.build_conflict(
        value,
        indicates,
        description=(
            f"Authoritative DACHFORM {raw!r} indicates {value}; the image evidence indicates "
            f"{indicates}."
        ),
    )
    extra = (
        {"image_conflict": confidence.penalty("image_conflict", cfg=cfg)} if conflict else {}
    )
    attribute = _authoritative(
        value,
        cap_key="roof_type_authoritative",
        method="authoritative_dachform",
        rationale=(
            "authoritative roof form for exactly this outline"
            + (
                "; reduced by a documented heuristic factor because the image evidence "
                "disagrees. That is not an estimate of the probability that the authoritative "
                "source is wrong; it flags a disagreement for human review"
                if conflict
                else ", and the image evidence does not contradict it"
            )
        ),
        source=source,
        source_detail={
            "layer": ROOF_POLYGON_SOURCE,
            "field": "DACHFORM",
            "raw_value": raw,
            "mapping": dict(DACHFORM_TO_ROOF_TYPE),
            "method": "authoritative_value",
        },
        limitations=(
            "binary flat/pitched; DACHFORM cannot express gable, hip, mansard or complex",
            "the roof may have changed between the 2023 model inputs and the 2024 imagery",
            *(
                (
                    "the conflict factor is a documented heuristic, not a probability that the "
                    "source is wrong",
                )
                if conflict
                else ()
            ),
        ),
        extra_factors=extra,
        cfg=cfg,
    )
    evidence = {
        "indicates": indicates,
        "method": orientation.RIDGE_METHOD,
        "rationale": rationale,
        "quality": dict(ridge.quality),
    }
    return Attribute(
        value=attribute.value,
        availability=attribute.availability,
        confidence=attribute.confidence,
        source_detail=attribute.source_detail,
        image_evidence=evidence,
        conflict=conflict,
    )


def build_attributes(
    record: RoofRecord,
    observations: dict[str, ImageObservation],
    metric: Any,
    typology: tuple[dict[str, Any] | None, float | None],
    building_info: dict[str, Any] | None,
    cfg: Config,
    sources: dict[str, str],
) -> dict[str, Attribute]:
    """Every published attribute for one building, each with its own provenance."""
    roof_source = sources[ROOF_POLYGON_SOURCE]
    imagery_source = sources[str(cfg.threshold("imagery", "layer"))]
    typology_layer, info_layer = wfs.LAYER_TYPOLOGY, wfs.LAYER_BUILDING_INFO
    typology_source, info_source = sources[typology_layer], sources[info_layer]
    props, out = record.raw_properties, {}

    out["roof_area_m2"] = Attribute(
        value=round(metric.area_m2, 2),
        availability=Availability.DERIVED,
        unit="m2",
        measure="planimetric",
        source_detail={
            "computed_from": PRIMARY_POLYGON,
            "polygon_source": ROOF_POLYGON_SOURCE,
            **metric.as_dict(),
        },
        confidence=confidence.build(
            Availability.DERIVED,
            "projected_area_epsg31256",
            f"planimetric area of the authoritative outline in {metric.crs}, interior rings "
            f"excluded",
            (roof_source,),
            {},
            (
                "planimetric: the horizontal projection, not the sloped roof surface area",
                "the reconnaissance cos(latitude) frame carries ~0.743% area error and is "
                "never used for a published value (design section 2, Amendment B3)",
            ),
            confidence.positive_cap("roof_area_m2", cfg=cfg),
            cfg=cfg,
        ),
    )

    out["roof_type"] = build_roof_type(record, observations["ridge_orientation_deg"], cfg, sources)

    out["roof_subtype"] = Attribute(
        value=None,
        availability=Availability.UNAVAILABLE,
        confidence=Confidence(
            method="not_implemented",
            rationale=(
                "gable versus hip is reported only on strong ridge evidence (design section 5) "
                "and no discriminator is implemented: one dominant ridge orientation cannot "
                "separate a gable from a hip. Null is the absence of a claim, not a negative."
            ),
            sources=(imagery_source,),
            score=None,
            limitations=("absence of a subtype is not a statement about the roof",),
        ),
    )

    out["mean_slope_deg"] = (
        _authoritative(
            round(float(record.slope_mean), 2),
            cap_key="mean_slope_deg",
            method="authoritative_single_record",
            rationale="taken directly from the single matched roof record; not area-weighted",
            source=roof_source,
            source_detail={
                "layer": ROOF_POLYGON_SOURCE,
                "field": "SLOPE_MEAN",
                "raw_value": record.slope_mean,
                "records_matched": 1,
                "aggregated": False,
            },
            limitations=(
                "mean over the whole roof, not per plane; a mansard or multi-plane roof can "
                "produce a mean that describes no actual plane",
            ),
            unit="deg",
            cfg=cfg,
        )
        if record.slope_mean is not None
        else _absent(
            ROOF_POLYGON_SOURCE,
            "SLOPE_MEAN",
            "the matched roof record carries no SLOPE_MEAN",
            roof_source,
        )
    )

    out["pv_potential_kwp"] = (
        _authoritative(
            record.anlagenleistung,
            cap_key="pv_potential_kwp",
            method="authoritative_anlagenleistung",
            rationale="modelled photovoltaic capacity for this roof; never installed capacity",
            source=roof_source,
            source_detail={
                "layer": ROOF_POLYGON_SOURCE,
                "field": "ANLAGENLEISTUNG",
                "raw_value": record.anlagenleistung,
            },
            limitations=(
                "modelled potential at 450 Wp per module with published reduction factors; "
                "existing installations were excluded from the potential by the source",
            ),
            unit="kWp",
            cfg=cfg,
        )
        if record.anlagenleistung is not None
        else _absent(
            ROOF_POLYGON_SOURCE,
            "ANLAGENLEISTUNG",
            "the matched roof record carries no ANLAGENLEISTUNG",
            roof_source,
        )
    )

    out["pv_potential_annual_yield_kwh"] = (
        _authoritative(
            {
                "POT_ERTRAG_Y_MIN": record.pot_ertrag_y_min,
                "POT_ERTRAG_Y_MAX": record.pot_ertrag_y_max,
            },
            cap_key="pv_potential_annual_yield_kwh",
            method="authoritative_yield_range",
            rationale="published as the source's range, not collapsed to a point value",
            source=roof_source,
            source_detail={
                "layer": ROOF_POLYGON_SOURCE,
                "fields": ["POT_ERTRAG_Y_MIN", "POT_ERTRAG_Y_MAX"],
                "YR": props.get("YR"),
                "note": (
                    "YR (annual irradiation) is null throughout the 2025 layer and is not used; "
                    "the yield range supersedes it (design section 1.2)"
                ),
            },
            limitations=("a modelled range, not a measurement of generation",),
            unit="kWh/yr",
            cfg=cfg,
        )
        if record.pot_ertrag_y_min is not None or record.pot_ertrag_y_max is not None
        else _absent(
            ROOF_POLYGON_SOURCE,
            "POT_ERTRAG_Y_MIN/POT_ERTRAG_Y_MAX",
            "the matched roof record carries no potential-yield range",
            roof_source,
        )
    )

    present = {k: v for k, v in record.suitability_m2.items() if v is not None}
    out["pv_suitability_area_m2"] = (
        _authoritative(
            {
                "source_fields": dict(record.suitability_m2),
                "labels": dict(SUITABILITY_LABELS),
                "total_m2": sum(float(v) for v in present.values()),
            },
            cap_key="pv_suitability_area_m2",
            method="authoritative_irradiation_class_areas",
            rationale=(
                "classified irradiation sub-areas in m2, source field names preserved; they "
                "cover only part of the roof outline"
            ),
            source=roof_source,
            source_detail={
                "layer": ROOF_POLYGON_SOURCE,
                "fields": list(SUITABILITY_FIELDS),
                "coverage_of_roof_area": round(
                    sum(float(v) for v in present.values()) / metric.area_m2, 4
                )
                if metric.area_m2 > 0
                else None,
            },
            limitations=(
                "sub-areas of the roof, not the roof area; areas below 600 kWh/m2/yr are "
                "excluded from the source's capacity model",
            ),
            unit="m2",
            cfg=cfg,
        )
        if present
        else _absent(
            ROOF_POLYGON_SOURCE,
            "/".join(SUITABILITY_FIELDS),
            "the matched roof record carries no irradiation-class areas",
            roof_source,
        )
    )

    out["address"] = (
        _authoritative(
            record.adresse,
            cap_key="address",
            method="authoritative_adresse",
            rationale="address string of the matched roof record",
            source=roof_source,
            source_detail={
                "layer": ROOF_POLYGON_SOURCE,
                "field": "ADRESSE",
                "raw_value": record.adresse,
                "STR": props.get("STR"),
                "ONR": props.get("ONR"),
                "BEZ": props.get("BEZ"),
            },
            limitations=(
                "ONR and BEZ change type between editions of this layer, so they are carried "
                "verbatim rather than parsed",
            ),
            cfg=cfg,
        )
        if record.adresse is not None
        else _absent(
            ROOF_POLYGON_SOURCE,
            "ADRESSE",
            "the matched roof record carries no ADRESSE",
            roof_source,
        )
    )

    # solar_panels is handled separately: its detector failed the completed reference evaluation,
    # so it
    # must not travel the from_image_observation path that turns an observation into a verdict.
    out["solar_panels"] = _withheld_solar_candidate(
        observations["solar_panels"], imagery_source
    )

    for name, cap_key, availability, unit in (
        ("roof_surface_class", "roof_surface_class", Availability.INFERRED, None),
        ("green_roof", "green_roof", Availability.OBSERVED, None),
        ("ridge_orientation_deg", "ridge_orientation", Availability.OBSERVED, "deg"),
        (
            "footprint_axis_orientation_deg",
            "footprint_axis_orientation_deg",
            Availability.DERIVED,
            "deg",
        ),
    ):
        obs = observations[name]
        out[name] = confidence.from_image_observation(
            obs,
            cap_key,
            availability=availability,
            unit=unit,
            sources=(imagery_source,),
            factors=_image_factors(obs, cfg),
            cfg=cfg,
        )

    typology_props, typology_share = typology
    for name, field, cap_key in (
        ("construction_epoch", TYPOLOGY_EPOCH_FIELD, "construction_epoch"),
        ("building_typology", TYPOLOGY_TYPE_FIELD, "building_typology"),
    ):
        raw = (typology_props or {}).get(field)
        placeholder = raw == TYPOLOGY_NO_VALUE_PLACEHOLDER
        out[name] = (
            _authoritative(
                raw,
                cap_key=cap_key,
                method="authoritative_typology_best_overlap",
                rationale=(
                    f"from the GEBAEUDETYPOGD polygon covering {typology_share:.0%} of the "
                    f"authoritative roof outline"
                ),
                source=typology_source,
                source_detail={
                    "layer": typology_layer,
                    "field": field,
                    "raw_value": raw,
                    "overlap_fraction_of_roof": round(float(typology_share), 4),
                    "matched_by": "best_geometric_overlap",
                    TYPOLOGY_EPOCH_SECONDARY: (typology_props or {}).get(
                        TYPOLOGY_EPOCH_SECONDARY
                    ),
                    "note": (
                        f"{TYPOLOGY_EPOCH_SECONDARY} is carried verbatim for reference; the "
                        f"published field is {TYPOLOGY_EPOCH_FIELD}, which is the one named in "
                        f"the design. No precedence rule is invented between them."
                    ),
                },
                limitations=(
                    "context only; the construction epoch is never used as a prior for roof "
                    "type (design section 5.1)",
                    "the typology cadastre is cut differently from the roof record, so the "
                    "match is a best geometric overlap and its share is published",
                ),
                extra_factors={
                    "typology_overlap": _typology_overlap_factor(typology_share, cfg)
                },
                cfg=cfg,
            )
            if raw is not None and not placeholder
            else _absent(
                typology_layer,
                field,
                (
                    "no GEBAEUDETYPOGD polygon overlaps this roof outline"
                    if typology_props is None
                    else (
                        f"the overlapping GEBAEUDETYPOGD polygon carries "
                        f"{TYPOLOGY_NO_VALUE_PLACEHOLDER!r} in {field}, which is the source "
                        f"declining to state a value rather than a value"
                    )
                    if placeholder
                    else f"the overlapping GEBAEUDETYPOGD polygon carries no {field}"
                ),
                typology_source,
                {
                    "raw_value": raw,
                    "overlap_fraction_of_roof": round(float(typology_share), 4),
                    "matched_by": "best_geometric_overlap",
                    TYPOLOGY_EPOCH_SECONDARY: (typology_props or {}).get(
                        TYPOLOGY_EPOCH_SECONDARY
                    ),
                    "note": (
                        f"raw_value is preserved verbatim so the placeholder stays traceable. "
                        f"{TYPOLOGY_EPOCH_SECONDARY} is carried for reference only and is "
                        f"never promoted into the published value: no precedence rule is "
                        f"invented between the two epoch columns."
                    ),
                }
                if typology_props is not None
                else None,
            )
        )

    for name, field, cap_key in (
        ("year_built", "BAUJAHR", "year_built"),
        ("architect", "ARCHITEKT", "architect"),
    ):
        raw = (building_info or {}).get(field)
        out[name] = (
            _authoritative(
                raw,
                cap_key=cap_key,
                method="authoritative_building_info_point_in_polygon",
                rationale="from the GEBAEUDEINFOOGD point inside the authoritative roof outline",
                source=info_source,
                source_detail={
                    "layer": info_layer,
                    "field": field,
                    "raw_value": raw,
                    "OBJECTID": (building_info or {}).get("OBJECTID"),
                    "matched_by": "point_in_authoritative_roof_polygon",
                },
                limitations=(
                    "GEBAEUDEINFOOGD covers historic stock; most of this study area is "
                    "post-2010 and carries no record",
                ),
                cfg=cfg,
            )
            if raw is not None
            else _absent(
                info_layer,
                field,
                (
                    "no GEBAEUDEINFOOGD point falls inside this roof outline"
                    if building_info is None
                    else f"the GEBAEUDEINFOOGD point inside this outline carries no {field}"
                ),
                info_source,
            )
        )

    return out


# --------------------------------------------------------------------------------------
# stage 8 - one building record
# --------------------------------------------------------------------------------------


def build_building(
    building_id: str,
    record: RoofRecord,
    evidence: JoinEvidence,
    cfg: Config,
    *,
    typology_features: list[dict[str, Any]],
    info_features: list[dict[str, Any]],
    fmzk_ids: dict[int, list[int]],
    sources: dict[str, str],
    write_overlays: bool = True,
) -> tuple[dict[str, Any], ImageCrop, CvDelineation]:
    """One published building record, plus the crop and delineation the overlay needs.

    ``write_overlays`` is threaded in only so that ``overlay`` names a file that exists. Under
    ``--no-overlays`` no PNG is written, so the field is ``null`` rather than a dangling path.
    """
    crs = str(cfg.threshold("crs", "metric"))
    _, metric = geometry.to_metric(record.geometry, crs)

    crop = imaging.build_crop(record.geometry, cfg.study_area.cache_dir, cfg)
    if crop.missing_tiles:
        logger.warning(
            "%s: %d tile(s) missing from the cache: %s",
            building_id,
            len(crop.missing_tiles),
            ", ".join(crop.missing_tiles[:3]),
        )
    cv = delineate(crop, record, cfg)
    observations = observe(crop, record, cfg)

    attributes = build_attributes(
        record,
        observations,
        metric,
        _best_overlapping(record.geometry, typology_features, cfg),
        _contained_point(record.geometry, info_features),
        cfg,
        sources,
    )

    delineation = {"primary_polygon": PRIMARY_POLYGON, **cv.as_dict()}
    building = {
        "building_id": building_id,
        "source_ids": {
            "anlagenleistung2025_objectid": record.objectid,
            "bw_geb_id": evidence.bw_geb_id,
            "fmzk_ids": fmzk_ids.get(evidence.bw_geb_id or -1, []),
        },
        "authoritative_roof_polygon": {
            "type": record.geometry["type"],
            "coordinates": record.geometry["coordinates"],
            "crs": WGS84,
            "polygon_source": ROOF_POLYGON_SOURCE,
        },
        "cv_candidate_polygon": (
            None
            if cv.polygon is None
            else {
                "type": cv.polygon["type"],
                "coordinates": cv.polygon["coordinates"],
                "crs": WGS84,
            }
        ),
        "delineation": delineation,
        "fmzk_crosscheck": evidence.as_dict(),
        "attributes": {name: attr.as_dict() for name, attr in attributes.items()},
        "review_flags": provenance.review_flags(
            cfg,
            roof_type_conflict=attributes["roof_type"].conflict,
            slope_mean_deg=record.slope_mean,
            join=evidence,
            boundary_alignment_warning=delineation.get("boundary_alignment_warning"),
        ),
        "overlay": f"overlays/{building_id}.png" if write_overlays else None,
    }
    return building, crop, cv


# --------------------------------------------------------------------------------------
# stage 9 - overlays
# --------------------------------------------------------------------------------------

OVERLAY_AUTHORITATIVE_BGR = (0, 220, 255)  # amber
OVERLAY_CV_BGR = (255, 64, 200)  # magenta
_FONT = cv2.FONT_HERSHEY_SIMPLEX


def _polygon_rings(polygon: dict[str, Any]) -> list[list[Any]]:
    """Exterior and interior rings of a GeoJSON Polygon or MultiPolygon, as coordinate lists."""
    coordinates = polygon["coordinates"]
    groups = [coordinates] if polygon.get("type") == "Polygon" else coordinates
    return [ring for group in groups for ring in group]


def _draw_rings(canvas: np.ndarray, crop: ImageCrop, polygon: dict[str, Any], colour: Any) -> None:
    for ring in _polygon_rings(polygon):
        pts = np.rint([imaging.lonlat_to_pixel(crop, c[0], c[1]) for c in ring])
        cv2.polylines(canvas, [pts.astype(np.int32)], True, colour, 2, cv2.LINE_AA)


# Hershey fonts are ASCII-only, so "Wiedner Gürtel" would render as "Wiedner G??rtel". The
# JSON keeps the address verbatim; only the drawn caption is transliterated, and it uses the
# German convention rather than dropping the diacritic.
_ASCII_FOLD = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "–": "-"}
)


def _ascii(text: str) -> str:
    return text.translate(_ASCII_FOLD).encode("ascii", "replace").decode("ascii")


def _wrap(text: str, width_px: int, scale: float) -> list[str]:
    """Break one caption line on spaces so it fits the image width. Nothing is truncated."""
    out: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if current and cv2.getTextSize(candidate, _FONT, scale, 1)[0][0] > width_px:
            out.append(current)
            current = word
        else:
            current = candidate
    return [*out, current] if current else out


def _label(canvas: np.ndarray, lines: list[str], legend: list[tuple[str, Any]]) -> np.ndarray:
    """Stack a caption under the image: wrapped text lines, then a colour legend."""
    scale, width = 0.45, canvas.shape[1]
    wrapped = [part for line in lines for part in _wrap(_ascii(line), width - 16, scale)]
    legend = [(_ascii(text), colour) for text, colour in legend]
    panel = np.zeros((20 * (len(wrapped) + len(legend)) + 16, width, 3), dtype=np.uint8)
    y = 20
    for line in wrapped:
        cv2.putText(panel, line, (8, y), _FONT, scale, (235, 235, 235), 1, cv2.LINE_AA)
        y += 20
    for text, colour in legend:
        cv2.line(panel, (10, y - 5), (34, y - 5), colour, 3, cv2.LINE_AA)
        cv2.putText(panel, text, (42, y), _FONT, scale, colour, 1, cv2.LINE_AA)
        y += 20
    return np.vstack([canvas, panel])


def _green_roof_caption(green_roof: bool | None) -> str:
    """Three states, not two. ``None`` is an abstention and must not read as a negative.

    The previous caption tested truthiness, which collapsed ``None`` into "not detected" — the
    exact conflation the abstention design exists to prevent. The JSON value and every threshold
    are untouched; this only controls the words drawn on the overlay.
    """
    if green_roof is None:
        return "unavailable / unclear (no usable RGB evidence)"
    if green_roof:
        return "detected (low confidence, dormant season)"
    return "not detected (low confidence, dormant season)"


def render_overlay(
    building: dict[str, Any], crop: ImageCrop, cv_result: CvDelineation
) -> np.ndarray:
    """Preliminary per-building overlay for Phase 3 visual inspection.

    Authoritative outline in amber, CV candidate in magenta, both labelled. Deliberately plain:
    it exists so a human can see which outline is which and whether the candidate wandered, not
    to be a figure. The caption names the primary polygon so the image cannot be misread as the
    CV stage having produced the delineation.
    """
    canvas = cv2.cvtColor(np.ascontiguousarray(crop.rgb), cv2.COLOR_RGB2BGR)
    _draw_rings(canvas, crop, building["authoritative_roof_polygon"], OVERLAY_AUTHORITATIVE_BGR)
    if cv_result.polygon is not None:
        _draw_rings(canvas, crop, cv_result.polygon, OVERLAY_CV_BGR)

    attributes = building["attributes"]

    def value(name: str) -> Any:
        return attributes[name]["value"]

    agree = (
        f"agreement IoU {cv_result.iou}, Hausdorff {cv_result.hausdorff_m} m"
        if cv_result.polygon is not None
        else f"no CV candidate: {cv_result.failure_reason}"
    )
    objectid = building["source_ids"]["anlagenleistung2025_objectid"]
    lines = [
        f"{building['building_id']}  OBJECTID {objectid}  {value('address')}",
        f"roof_type {value('roof_type')} (DACHFORM "
        f"{attributes['roof_type']['source_detail']['raw_value']})"
        f"  slope {value('mean_slope_deg')} deg"
        f"  area {value('roof_area_m2')} m2 (EPSG:31256, planimetric)",
        f"{agree}  |  agreement between related estimates, not accuracy",
        # The JSON value and every threshold are unchanged; only the caption is. An unqualified
        # "green_roof False" on a dormant-season image reads as a finding, which is exactly what
        # design section 5.2 says it is not.
        f"surface {value('roof_surface_class')}"
        f"  green-roof RGB evidence: {_green_roof_caption(value('green_roof'))}"
        f"  ridge {value('ridge_orientation_deg')}",
        "solar_panels: WITHHELD - detector failed its reference evaluation, no Boolean published",
        "Datenquelle: Stadt Wien - data.wien.gv.at (CC BY 4.0)",
    ]
    if building["review_flags"]:
        triggers = ", ".join(flag["trigger"] for flag in building["review_flags"])
        lines.insert(-1, f"REQUIRES VISUAL REVIEW: {triggers}")
    legend = [
        ("authoritative_roof_polygon (PRIMARY, official geodata)", OVERLAY_AUTHORITATIVE_BGR),
        ("cv_candidate_polygon (evidence only, never primary)", OVERLAY_CV_BGR),
    ]
    return _label(canvas, lines, legend)


# --------------------------------------------------------------------------------------
# stage 10 - schema validation and the run itself
# --------------------------------------------------------------------------------------


def packaged_schema() -> dict[str, Any]:
    """The output schema as shipped **inside the package**, read via ``importlib.resources``.

    A path relative to ``__file__`` happens to work from a source checkout, so it would never
    catch a packaging mistake; the resource API is the call an installed wheel makes. If the
    schema is not packaged, this raises here rather than at the moment of writing output.
    """
    text = (
        resources.files(SCHEMA_PACKAGE).joinpath(SCHEMA_RESOURCE).read_text(encoding="utf-8")
    )
    return json.loads(text)


def validate_output(document: dict[str, Any]) -> None:
    """Validate the whole document against the packaged schema. Raises on any violation.

    Two steps on purpose: the packaged resource is compared with the schema the validator
    holds, so a wheel shipping a stale or missing schema fails loudly, and then
    ``schema.validate`` reports every failing JSON path rather than only the first.
    """
    if packaged_schema() != schema.load_schema():
        raise PipelineError(
            f"the schema packaged as {SCHEMA_PACKAGE}/{SCHEMA_RESOURCE} differs from the one "
            f"the validator loaded; the output would be checked against a different contract "
            f"than the installed package ships"
        )
    schema.validate(document)


def run(
    cfg: Config | None = None,
    *,
    generated_at: datetime | date | str | None = None,
    output_dir: Path | str | None = None,
    write_overlays: bool = True,
) -> dict[str, Any]:
    """Run the whole pipeline offline and return the validated output document.

    ``generated_at`` is the only **intentionally variable** field in the document, and it is a
    parameter so that it can be pinned and two runs compared byte for byte. Pinning it does not
    make the comparison a reproducibility guarantee: source code, dependency binaries, the
    interpreter/runtime and the numerical libraries underneath (GEOS, OpenCV, NumPy) can all
    still affect byte-level output, and none of them is captured by the run fingerprints.

    When ``output_dir`` is given, the validated document and the per-building overlays are
    written there — **after** validation, so a failing run never leaves a document behind that
    looks like a result.
    """
    cfg = cfg or load_config()
    require_verified_cache(cfg)

    records = load_roof_records(cfg)
    pinned = select_pinned(records, cfg)
    logger.info("%d cached roof records; %d pinned", len(records), len(pinned))

    evidences, fmzk_ids = join_units(records, cfg)
    typology_features = _read_features(cfg.study_area.cache_dir / TYPOLOGY_FILE)
    info_features = _read_features(cfg.study_area.cache_dir / BUILDING_INFO_FILE)
    sources = source_names(cfg)

    buildings: list[dict[str, Any]] = []
    overlays: dict[str, np.ndarray] = {}
    for building_id, record in pinned.items():
        building, crop, cv_result = build_building(
            building_id,
            record,
            evidences[record.objectid],
            cfg,
            typology_features=typology_features,
            info_features=info_features,
            fmzk_ids=fmzk_ids,
            sources=sources,
            write_overlays=write_overlays,
        )
        buildings.append(building)
        if write_overlays:
            overlays[building_id] = render_overlay(building, crop, cv_result)
        logger.info(
            "%s: roof_type=%s cv_iou=%s cardinality=%s review_flags=%d",
            building_id,
            building["attributes"]["roof_type"]["value"],
            cv_result.iou,
            evidences[record.objectid].cardinality,
            len(building["review_flags"]),
        )

    expected = len(cfg.study_area.buildings)
    ids = [b["building_id"] for b in buildings]
    if len(buildings) != expected or len(set(ids)) != expected:
        raise PipelineError(
            f"expected {expected} unique building records, produced {len(buildings)} "
            f"({len(set(ids))} unique)"
        )

    document = {
        "run": provenance.run_block(cfg, generated_at or datetime.now().astimezone()),
        "buildings": buildings,
    }
    validate_output(document)

    if output_dir is not None:
        write_outputs(document, overlays, output_dir)
    return document


def write_outputs(
    document: dict[str, Any], overlays: dict[str, np.ndarray], output_dir: Path | str
) -> Path:
    """Write the validated document and the overlays. Called only after validation passes."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "roof_attributes.json"
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    if overlays:
        overlay_dir = output_dir / "overlays"
        overlay_dir.mkdir(parents=True, exist_ok=True)
        for building_id, image in overlays.items():
            overlay_path = overlay_dir / f"{building_id}.png"
            # cv2.imwrite returns False on failure rather than raising, so an unwritable path or
            # a full disk would otherwise leave the CLI exiting 0 with overlays missing - the
            # exact "green run, invalid output" case the exit-code contract exists to prevent.
            if not cv2.imwrite(str(overlay_path), image):
                raise PipelineError(f"failed to write overlay {overlay_path}")
    return path
