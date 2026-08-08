"""The offline, cache-backed run: cached sources in, ``roof_attributes.json`` out.

This module is **wiring**. Every measurement, threshold and judgement already lives in a
module built for it, and nothing here re-implements one. What it does own is the order of the
stages and the rules that only make sense between them:

* **The cache is verified before any building is touched** (:func:`verify_cache`, which calls
  ``propx_roofs.cache_build.verify`` rather than repeating it). A run against a cache with a
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
* **Failure isolation has a documented boundary (RTI-027).** A building whose record cannot
  be produced at all stays **fatal**: geometry, join and authoritative-source failures mean
  the inputs are broken, and publishing nine records where ten were promised (or a record
  built on broken inputs) would be worse than a red run — ``select_pinned`` additionally
  rejects a pinned record whose geometry is invalid or empty before any work is done. But an
  unexpected exception inside **one attribute's image path** (:func:`observe`) degrades that
  attribute to an abstention with a ``processing_error`` review flag and a logged traceback,
  rather than costing the other attributes and the other nine buildings. The abstention is a
  report about the pipeline, never a finding about the roof.

The output is validated against the schema **shipped inside the package** before the caller is
told anything succeeded, and written **atomically**: the whole run is staged in a temporary
sibling directory, verified complete, fingerprinted in ``artifact_manifest.json``, and only
then swapped into place (see :func:`write_outputs` for the precise guarantee).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from shapely.geometry import shape
from shapely.validation import explain_validity

from . import audit, confidence, config, geometry, imaging, join, provenance, schema, units
from .attributes import judgeable_mask, orientation, roof_form, solar, surface, vegetation
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
    study_area_path: Path | str | None = None,
    pipeline_path: Path | str | None = None,
    cache_root: Path | str | None = None,
) -> Config:
    """Both config files, with the metric-CRS invariant already enforced by ``config.load``.

    ``cache_root`` overrides where ``data/cache/<area>/`` is looked for — the explicit escape
    hatch for an installed wheel running outside a checkout (RTI-002). It is plumbed through
    :func:`config.load` into the returned :class:`Config` rather than mutated globally.
    """
    kwargs: dict[str, Any] = {}
    if study_area_path is not None:
        kwargs["study_area_path"] = study_area_path
    if pipeline_path is not None:
        kwargs["pipeline_path"] = pipeline_path
    if cache_root is not None:
        kwargs["cache_root"] = cache_root
    return config.load(**kwargs)


def verify_cache(cfg: Config) -> list[str]:
    """Problems found in the committed cache; empty means fit to run. Offline.

    The verifier is reused from :mod:`propx_roofs.cache_build` rather than reimplemented: a
    second copy of "is this cache fit to run" would drift from the one that built the cache,
    and the drift would show up as a run that passes its own check and fails the tool's. The
    builder used to live only at ``tools/build_cache.py`` and was imported here by file path,
    which broke for an installed wheel with no checkout (RTI-002); it is now a normal module.
    """
    from . import cache_build

    cache_dir = cfg.study_area.cache_dir
    if not cache_dir.is_dir():
        # The cache is real fetched data and cannot be shipped inside the wheel, so an
        # installed package without a checkout must be pointed at it explicitly.
        raise PipelineError(
            f"cache directory not found at {cache_dir}. The cache is not packaged with the "
            f"wheel; point the run at a checkout's data with --cache-root, or set "
            f"{config.DATA_ROOT_ENV} to a directory containing data/cache/, or rebuild the "
            f"cache with `propx-roofs cache-build` (the one networked command)."
        )
    return list(cache_build.verify(cfg))


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
            + "\nRebuild it with `propx-roofs cache-build`, which fetches from the live "
            "services. The pipeline itself never does."
        )
    logger.info("cache verified: %s", cfg.study_area.cache_dir)


# --------------------------------------------------------------------------------------
# stage 2 - authoritative roof records
# --------------------------------------------------------------------------------------


def _read_features(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise PipelineError(f"missing cached layer {path}; run `propx-roofs cache-build`")
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

    A pinned record whose geometry cannot be parsed, is empty, or is invalid per shapely is
    equally fatal, and it is rejected **here**, naming the building id (RTI-027): the cache
    verifier checks the same property, but the pipeline must not depend on the caller having
    run it, and a broken outline discovered later would surface as an unrelated exception
    deep inside some stage instead of as "this input is broken".
    """
    missing = [b.objectid for b in cfg.study_area.buildings if b.objectid not in records]
    if missing:
        raise PipelineError(
            f"pinned roof record(s) {sorted(missing)} are not in "
            f"{cfg.study_area.cache_dir / ROOF_FILE}. The cache and configs/study_area.yaml "
            f"disagree about the sample; refusing to publish a partial one."
        )
    for building in cfg.study_area.buildings:
        record = records[building.objectid]
        try:
            geom = shape(record.geometry)
        except Exception as error:
            raise PipelineError(
                f"{building.building_id} (OBJECTID {building.objectid}): pinned roof geometry "
                f"cannot be parsed ({type(error).__name__}: {error}). Broken input geometry is "
                f"fatal; rebuild or repair the cache before running."
            ) from error
        if geom.is_empty:
            raise PipelineError(
                f"{building.building_id} (OBJECTID {building.objectid}): pinned roof geometry "
                f"is empty. An empty outline cannot produce a record, and publishing a partial "
                f"sample is refused; rebuild or repair the cache before running."
            )
        if not geom.is_valid:
            raise PipelineError(
                f"{building.building_id} (OBJECTID {building.objectid}): pinned roof geometry "
                f"is invalid ({explain_validity(geom)}). Every published area, join and "
                f"observation would be built on it, so this is fatal; rebuild or repair the "
                f"cache before running."
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


def _degraded_observation(name: str, error: Exception) -> ImageObservation:
    """The abstention a failed observation stage degrades to. A report, not a finding."""
    return ImageObservation(
        name=name,
        value=None,
        method="processing_error_degraded_to_abstention",
        rationale=(
            f"the {name} observation raised an unexpected "
            f"{type(error).__name__} ({error}) and was degraded to an abstention; no value is "
            f"asserted and this null describes a pipeline failure, not the roof"
        ),
        quality={"processing_error": f"{type(error).__name__}: {error}"},
        limitations=(
            "processing_error: this abstention reports an unexpected exception in this "
            "attribute's image path, not an observation of the roof",
        ),
    )


def observe(
    crop: ImageCrop, record: RoofRecord, cfg: Config
) -> tuple[dict[str, ImageObservation], dict[str, str]]:
    """Every image-derived observation for one roof, each still an abstention-capable value.

    Failure isolation (RTI-027): an unexpected exception inside **one** observation stage is
    degraded to an abstention carrying a ``processing_error`` note — and returned in the
    second mapping so the building record gains a ``processing_error`` review flag — instead
    of aborting the whole ten-building run. The traceback is logged. This wrapper covers only
    the per-attribute image paths; geometry, join and authoritative-source failures remain
    fatal because they mean the inputs are broken (see the module docstring).
    """
    observations: dict[str, ImageObservation] = {}
    errors: dict[str, str] = {}

    def attempt(names: tuple[str, ...], builder: Any) -> None:
        try:
            results = builder()
        except Exception as error:  # noqa: BLE001 - degraded to a reportable abstention
            logger.exception(
                "observation stage for %s raised; degrading to an abstention", "/".join(names)
            )
            for name in names:
                observations[name] = _degraded_observation(name, error)
                errors[name] = f"{type(error).__name__}: {error}"
        else:
            for name, obs in zip(names, results, strict=True):
                observations[name] = obs

    attempt(
        ("visual_surface_appearance",), lambda: (surface.observe_surface_appearance(crop, cfg),)
    )
    attempt(("solar_panels",), lambda: (solar.observe_solar_panels(crop, cfg),))
    attempt(("green_roof",), lambda: (vegetation.observe_green_roof(crop, cfg),))
    # No standalone vegetated_fraction observation: the measurement is already published as
    # green_roof's image_evidence.quality.vegetated_fraction, and calling
    # vegetation.observe_vegetated_fraction here only ran the shared index pass a second
    # time per building to produce a value nothing below reads.
    # ridge and axis come from one orientation pass, so a failure there degrades both.
    attempt(
        ("ridge_orientation_deg", "footprint_axis_orientation_deg"),
        lambda: orientation.observe(crop, record.geometry, cfg),
    )
    # The symmetric flat-versus-pitched evidence (RTI-005): "pitched" on a gated ridge,
    # "flat" only on positive flatness evidence, None otherwise. Not a published attribute of
    # its own - it becomes roof_type's image_evidence, so both conflict directions can fire.
    attempt(
        ("image_roof_form",),
        lambda: (roof_form.observe_roof_form(crop, cfg, record.geometry),),
    )
    return observations, errors


# --------------------------------------------------------------------------------------
# stage 6 - authoritative enrichment by spatial join
# --------------------------------------------------------------------------------------


# How many enrichment candidates are recorded verbatim in source_detail. Enough to show every
# genuine competitor on this cadastre (a roof rarely straddles more than a handful of typology
# polygons) without serialising a whole city block of slivers.
ENRICHMENT_CANDIDATES_RECORDED = 5

# The two GEBAEUDEINFOOGD fields the pipeline publishes; the point-join ambiguity test asks
# whether candidates disagree on exactly these, because a disagreement on an unpublished field
# changes nothing a reader sees.
INFO_PUBLISHED_FIELDS = ("BAUJAHR", "ARCHITEKT")

POINT_SELECTION_RULE = "nearest_to_roof_centroid_in_metric_crs"


@dataclass(frozen=True)
class TypologyMatch:
    """The best-overlap typology enrichment for one roof, with the evidence behind it.

    ``properties`` is ``None`` when no polygon overlaps at all. ``below_floor`` means the
    best candidate exists but covers less than ``join.typology_min_overlap`` of the roof, so
    its attributes are withheld rather than published. ``ambiguous`` means a second candidate
    covers a similar share (``join.typology_ambiguity_margin``); it is recorded regardless of
    the floor, but only a *published* ambiguous value earns a review flag.
    """

    properties: dict[str, Any] | None
    share_of_roof: float | None  # intersection area / roof area
    share_of_source: float | None  # intersection area / typology polygon area
    candidate_count: int
    candidates: tuple[dict[str, Any], ...]  # top candidates, best first
    below_floor: bool
    ambiguous: bool
    ambiguity_reason: str | None
    min_overlap: float
    ambiguity_margin: float


def match_typology(
    roof_geom_wgs84: Any, features: list[dict[str, Any]], cfg: Config
) -> TypologyMatch:
    """The typology feature covering most of the roof outline, measured in EPSG:31256.

    Best overlap, not centroid containment: typology polygons are cut on a different cadastre
    and a roof centroid can land in a neighbouring parcel. Two shares are measured and both
    published (RTI-012): intersection over the **roof** (how much of this roof the polygon
    describes — the one the floor is applied to) and intersection over the **source polygon**
    (how much of the polygon lies on this roof — near 1.0 for a sliver of the roof that is
    entirely someone else's building, which is exactly why one share alone misleads).

    The floor ``join.typology_min_overlap`` is a hard abstention threshold: below it the
    best candidate's attributes are withheld (published null with ``not_in_source``
    semantics) instead of published with a discounted confidence. The margin
    ``join.typology_ambiguity_margin`` marks two candidates with similar shares as an
    ambiguous enrichment. Both are documented heuristics from ``configs/pipeline.yaml``.
    """
    crs = str(cfg.threshold("crs", "metric"))
    min_overlap = float(cfg.threshold("join", "typology_min_overlap"))
    margin = float(cfg.threshold("join", "typology_ambiguity_margin"))

    roof_wgs84 = geometry.as_shapely(roof_geom_wgs84)
    roof_metric, roof_measure = geometry.to_metric(roof_wgs84, crs)
    found: list[tuple[float, float, dict[str, Any]]] = []
    if roof_measure.area_m2 > 0:
        for feature in features:
            if not feature.get("geometry"):
                continue
            other = shape(feature["geometry"])
            if not roof_wgs84.intersects(other):
                continue
            other_metric, other_measure = geometry.to_metric(other, crs)
            overlap = roof_metric.intersection(other_metric).area
            if overlap <= 0:
                continue
            share_roof = overlap / roof_measure.area_m2
            share_source = (
                overlap / other_measure.area_m2 if other_measure.area_m2 > 0 else None
            )
            found.append((share_roof, share_source, dict(feature.get("properties") or {})))
    # OBJECTID breaks share ties so the same input always yields the same winner.
    found.sort(key=lambda c: (-c[0], c[2].get("OBJECTID") or 0))

    candidates = tuple(
        {
            "OBJECTID": props.get("OBJECTID"),
            "overlap_fraction_of_roof": round(share_roof, 4),
            "overlap_fraction_of_source_polygon": (
                round(share_source, 4) if share_source is not None else None
            ),
        }
        for share_roof, share_source, props in found[:ENRICHMENT_CANDIDATES_RECORDED]
    )

    if not found:
        return TypologyMatch(
            properties=None,
            share_of_roof=None,
            share_of_source=None,
            candidate_count=0,
            candidates=(),
            below_floor=False,
            ambiguous=False,
            ambiguity_reason=None,
            min_overlap=min_overlap,
            ambiguity_margin=margin,
        )

    best_share, best_source_share, best_props = found[0]
    ambiguity_reason = None
    if len(found) > 1 and best_share > 0:
        runner_share = found[1][0]
        ratio = runner_share / best_share
        if ratio > margin:
            ambiguity_reason = (
                f"the second-best GEBAEUDETYPOGD polygon (OBJECTID {found[1][2].get('OBJECTID')}) "
                f"covers {runner_share:.1%} of the roof outline, {ratio:.2f} of the best "
                f"candidate's {best_share:.1%} and above the ambiguity margin {margin:.2f}: "
                f"two typology polygons compete for this roof."
            )
    return TypologyMatch(
        properties=best_props,
        share_of_roof=best_share,
        share_of_source=best_source_share,
        candidate_count=len(found),
        candidates=candidates,
        below_floor=best_share < min_overlap,
        ambiguous=ambiguity_reason is not None,
        ambiguity_reason=ambiguity_reason,
        min_overlap=min_overlap,
        ambiguity_margin=margin,
    )


@dataclass(frozen=True)
class PointMatch:
    """The building-info point join for one roof, with the decision recorded (RTI-013).

    ``candidate_count`` is recorded even when 0 or 1 so the join is auditable. With several
    candidates the deterministic domain rule is *nearest to the roof polygon's centroid in
    the metric CRS* (OBJECTID as the final tie-break); the chosen point is published, and
    ``ambiguous`` marks a near-tie (within ``join.point_tie_epsilon_m``) or a material
    disagreement between candidates on a published field — both flagged, never silently
    resolved.
    """

    properties: dict[str, Any] | None
    candidate_count: int
    candidates: tuple[dict[str, Any], ...]  # nearest first: OBJECTID + distance
    chosen_objectid: Any
    selection_rule: str
    ambiguous: bool
    ambiguity_reason: str | None


def match_info_point(
    roof_geom_wgs84: Any, features: list[dict[str, Any]], cfg: Config
) -> PointMatch:
    """The building-info point on the roof outline, boundary included.

    ``covers`` rather than ``contains``: a point lying exactly on the outline's boundary is
    the source describing this building, and ``contains`` (boundary-exclusive) silently
    dropped it. Several points under one outline mean several addressed buildings under one
    roof record; nothing is merged — the nearest point to the roof centroid (in the metric
    CRS) is chosen deterministically, every candidate and its distance is recorded, and a
    near-tie or a material value disagreement is reported as ambiguous so the caller can
    flag it.
    """
    crs = str(cfg.threshold("crs", "metric"))
    epsilon_m = float(cfg.threshold("join", "point_tie_epsilon_m"))
    roof = geometry.as_shapely(roof_geom_wgs84)
    roof_metric, _ = geometry.to_metric(roof, crs)
    centroid = roof_metric.centroid

    found: list[tuple[float, dict[str, Any]]] = []
    for feature in features:
        if not feature.get("geometry"):
            continue
        point = shape(feature["geometry"])
        if not roof.covers(point):
            continue
        point_metric, _ = geometry.to_metric(point, crs)
        found.append(
            (float(point_metric.distance(centroid)), dict(feature.get("properties") or {}))
        )
    found.sort(key=lambda c: (c[0], c[1].get("OBJECTID") or 0))

    candidates = tuple(
        {"OBJECTID": props.get("OBJECTID"), "distance_to_roof_centroid_m": round(distance, 2)}
        for distance, props in found
    )
    if not found:
        return PointMatch(
            properties=None,
            candidate_count=0,
            candidates=(),
            chosen_objectid=None,
            selection_rule=POINT_SELECTION_RULE,
            ambiguous=False,
            ambiguity_reason=None,
        )

    reasons = []
    if len(found) > 1:
        nearest, runner_up = found[0][0], found[1][0]
        if runner_up - nearest < epsilon_m:
            reasons.append(
                f"the two nearest GEBAEUDEINFOOGD points are {nearest:.2f} m and "
                f"{runner_up:.2f} m from the roof centroid, within the "
                f"{epsilon_m:g} m tie epsilon, so the distance ordering does not distinguish "
                f"them"
            )
        for field in INFO_PUBLISHED_FIELDS:
            values = {props.get(field) for _, props in found if props.get(field) is not None}
            if len(values) > 1:
                reasons.append(
                    f"the {len(found)} candidate points disagree on published field {field} "
                    f"({sorted(map(str, values))})"
                )
    reason = (
        f"{len(found)} GEBAEUDEINFOOGD points fall on this roof outline and the join is "
        f"ambiguous: " + "; ".join(reasons) + "."
        if reasons
        else None
    )
    return PointMatch(
        properties=found[0][1],
        candidate_count=len(found),
        candidates=candidates,
        chosen_objectid=found[0][1].get("OBJECTID"),
        selection_rule=POINT_SELECTION_RULE,
        ambiguous=reason is not None,
        ambiguity_reason=reason,
    )


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


def _evidence_factors(name: str, obs: ImageObservation, cfg: Config) -> dict[str, float]:
    """Evidence-sensitive penalty factors for one determined image observation (RTI-008).

    Each factor is :func:`confidence.evidence_factor` applied to a **measured** quality
    quantity the observation itself published, under the factor's own name from
    ``confidence.evidence_factor`` in ``configs/pipeline.yaml`` — so a reader can look the
    factor name up in the config, find the measured input in ``image_evidence.quality``, and
    reproduce the score by hand. Derived inputs that do not already appear in the quality dict
    (the mixed-verdict pair share, the vegetation index margin) are written into it here for
    the same reason.

    Applied only to determined values: an abstention carries no score, so a factor on it would
    be arithmetic on a number that does not exist. The former fixed ``shadow_heavy`` step
    penalty is subsumed by the continuous judgeability factors (see ``configs/pipeline.yaml``).
    ``footprint_axis_orientation_deg`` earns no factor: it is computed from the authoritative
    outline's geometry, not from pixels, so no per-building pixel quality describes it.
    """
    if obs.value is None:
        return {}
    quality = obs.quality
    factors: dict[str, float] = {}

    if name == "visual_surface_appearance":
        judgeable = quality.get("judgeable_fraction")
        if judgeable is not None:
            factors["judgeable_fraction"] = confidence.evidence_factor(
                "judgeable_fraction", float(judgeable), cfg=cfg
            )
        margin = quality.get("margin_over_runner_up")
        dominance = quality.get("dominance_share")
        if obs.value == "mixed":
            # A mixed verdict rests on the two signatures' combined share, not the margin
            # between them. pair_share = top + runner-up = 2*dominance - margin.
            if dominance is not None and margin is not None:
                pair_share = round(2.0 * float(dominance) - float(margin), 4)
                quality["pair_share"] = pair_share
                factors["surface_pair_share"] = confidence.evidence_factor(
                    "surface_pair_share", pair_share, cfg=cfg
                )
        elif margin is not None:
            factors["surface_margin"] = confidence.evidence_factor(
                "surface_margin", float(margin), cfg=cfg
            )
    elif name == "green_roof":
        vegetation_judgeable = quality.get("vegetation_judgeable_fraction")
        if vegetation_judgeable is not None:
            factors["vegetation_judgeable_fraction"] = confidence.evidence_factor(
                "vegetation_judgeable_fraction", float(vegetation_judgeable), cfg=cfg
            )
        vegetated = quality.get("vegetated_fraction")
        min_coverage = float(cfg.threshold("image", "green_roof", "min_coverage_fraction"))
        if vegetated is not None and min_coverage > 0:
            index_margin = round(abs(float(vegetated) - min_coverage) / min_coverage, 4)
            quality["vegetation_index_margin"] = index_margin
            factors["vegetation_index_margin"] = confidence.evidence_factor(
                "vegetation_index_margin", index_margin, cfg=cfg
            )
    elif name == "ridge_orientation_deg":
        gate_margin = quality.get("gate_margin")
        if gate_margin is not None:
            factors["ridge_gate_margin"] = confidence.evidence_factor(
                "ridge_gate_margin", float(gate_margin), cfg=cfg
            )
    return factors


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
    record: RoofRecord, form: ImageObservation, cfg: Config, sources: dict[str, str]
) -> Attribute:
    """``roof_type`` from ``DACHFORM``, with the image reading and any conflict kept separate.

    Three things stay independently visible and are never resolved into one number
    (design section 5.1): the authoritative class unmodified, what the imagery indicates as its
    own observation, and a conflict flag when the two disagree. The image value is never
    promoted and the authoritative value is never corrected.

    ``form`` is the symmetric roof-form observation (RTI-005):
    :func:`propx_roofs.attributes.roof_form.observe_roof_form` can indicate ``"pitched"`` (a
    fully gated ridge), ``"flat"`` (positive flatness evidence measured on judgeable pixels),
    or abstain — so **both** disagreement directions can fire the conflict flag, where the
    previous ridge-only evidence made authoritative-pitched-versus-flat-imagery structurally
    unreachable. ``provenance.build_conflict`` is value-symmetric and treats an image ``None``
    as agreement-by-abstention; an authoritative ``unknown`` conflicts with nothing — the
    image evidence then stands alone in ``image_evidence`` without a flag, because
    disagreement with an unknown is not a disagreement.

    ``complex`` is not reachable: the design permits it only on additional evidence of multiple
    inconsistent ridge directions or multi-modal plane brightness, and no such detector is
    implemented. Emitting it from ``DACHFORM``, which is binary, would be an invented class.
    """
    source = sources[ROOF_POLYGON_SOURCE]
    raw = record.dachform
    value = DACHFORM_TO_ROOF_TYPE.get(raw, "unknown") if raw is not None else "unknown"
    indicates, rationale = form.value, form.rationale
    conflict = (
        provenance.build_conflict(
            value,
            indicates,
            description=(
                f"Authoritative DACHFORM {raw!r} indicates {value}; the image evidence "
                f"indicates {indicates}."
            ),
        )
        # An authoritative "unknown" is the source declining to answer; the image evidence is
        # published either way and stands alone rather than "conflicting" with a non-answer.
        if value in DACHFORM_TO_ROOF_TYPE.values()
        else None
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
        "method": form.method,
        "rationale": rationale,
        # The full roof-form quality block: the signed margin in units of the deciding
        # threshold, every measured flatness/ridge feature, and the embedded gated-ridge
        # outcome - so the conflict (or its absence) is reproducible from the record.
        "quality": dict(form.quality),
    }
    return Attribute(
        value=attribute.value,
        availability=attribute.availability,
        confidence=attribute.confidence,
        source_detail=attribute.source_detail,
        image_evidence=evidence,
        conflict=conflict,
        requires_review=bool(conflict),
        review_reasons=(
            (
                "the authoritative class and the image roof-form evidence disagree (see "
                "conflict); the authoritative value is published unchanged and a human should "
                "decide which evidence to trust",
            )
            if conflict
            else ()
        ),
    )


def build_attributes(
    record: RoofRecord,
    observations: dict[str, ImageObservation],
    metric: Any,
    typology: TypologyMatch,
    building_info: PointMatch,
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

    out["roof_type"] = build_roof_type(record, observations["image_roof_form"], cfg, sources)

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
        ("visual_surface_appearance", "visual_surface_appearance", Availability.INFERRED, None),
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
            factors=_evidence_factors(name, obs, cfg),
            cfg=cfg,
        )

    # The join decision travels into source_detail of every enrichment attribute — present or
    # absent — so a reader can audit which polygons/points competed and why one was chosen or
    # everything was withheld (RTI-012).
    typology_join_detail: dict[str, Any] = {
        "matched_by": "best_geometric_overlap",
        "candidate_count": typology.candidate_count,
        "candidates": [dict(c) for c in typology.candidates],
        "min_overlap_fraction_of_roof": typology.min_overlap,
        "overlap_ambiguity_margin": typology.ambiguity_margin,
    }
    if typology.properties is not None:
        typology_join_detail["overlap_fraction_of_roof"] = round(
            float(typology.share_of_roof), 4
        )
        typology_join_detail["overlap_fraction_of_source_polygon"] = (
            round(float(typology.share_of_source), 4)
            if typology.share_of_source is not None
            else None
        )
    if typology.ambiguous:
        typology_join_detail["ambiguous"] = True
        typology_join_detail["ambiguity_reason"] = typology.ambiguity_reason

    for name, field, cap_key in (
        ("construction_epoch", TYPOLOGY_EPOCH_FIELD, "construction_epoch"),
        ("building_typology", TYPOLOGY_TYPE_FIELD, "building_typology"),
    ):
        raw = (typology.properties or {}).get(field)
        placeholder = raw == TYPOLOGY_NO_VALUE_PLACEHOLDER
        publishable = (
            typology.properties is not None
            and not typology.below_floor
            and raw is not None
            and not placeholder
        )
        if publishable:
            out[name] = _authoritative(
                raw,
                cap_key=cap_key,
                method="authoritative_typology_best_overlap",
                rationale=(
                    f"from the GEBAEUDETYPOGD polygon covering {typology.share_of_roof:.0%} of "
                    f"the authoritative roof outline (at or above the "
                    f"{typology.min_overlap:.0%} minimum overlap, a documented heuristic)"
                ),
                source=typology_source,
                source_detail={
                    "layer": typology_layer,
                    "field": field,
                    "raw_value": raw,
                    **typology_join_detail,
                    TYPOLOGY_EPOCH_SECONDARY: (typology.properties or {}).get(
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
                    "typology_overlap": _typology_overlap_factor(typology.share_of_roof, cfg)
                },
                cfg=cfg,
            )
        else:
            out[name] = _absent(
                typology_layer,
                field,
                (
                    "no GEBAEUDETYPOGD polygon overlaps this roof outline"
                    if typology.properties is None
                    else (
                        f"the best-overlapping GEBAEUDETYPOGD polygon covers only "
                        f"{typology.share_of_roof:.1%} of the authoritative roof outline, "
                        f"below the {typology.min_overlap:.0%} minimum "
                        f"(join.typology_min_overlap): a polygon describing less than half of "
                        f"the roof more likely describes a neighbouring building, so the value "
                        f"is withheld rather than published. The floor is a documented "
                        f"heuristic, not a calibrated figure"
                    )
                    if typology.below_floor
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
                    **typology_join_detail,
                    TYPOLOGY_EPOCH_SECONDARY: (typology.properties or {}).get(
                        TYPOLOGY_EPOCH_SECONDARY
                    ),
                    "note": (
                        f"raw_value is preserved verbatim so the withheld or placeholder "
                        f"content stays traceable and is never silently promoted. "
                        f"{TYPOLOGY_EPOCH_SECONDARY} is carried for reference only and is "
                        f"never promoted into the published value: no precedence rule is "
                        f"invented between the two epoch columns."
                    ),
                }
                if typology.properties is not None
                else dict(typology_join_detail),
            )

    # The point-join decision, recorded on both attributes whether or not a value came out
    # (RTI-013): candidate count, every candidate's distance, the chosen point and the rule.
    info_join_detail: dict[str, Any] = {
        "matched_by": "point_covered_by_authoritative_roof_polygon",
        "candidate_count": building_info.candidate_count,
        "candidates": [dict(c) for c in building_info.candidates],
        "selection_rule": building_info.selection_rule,
    }
    if building_info.ambiguous:
        info_join_detail["ambiguous"] = True
        info_join_detail["ambiguity_reason"] = building_info.ambiguity_reason

    for name, field, cap_key in (
        ("year_built", "BAUJAHR", "year_built"),
        ("architect", "ARCHITEKT", "architect"),
    ):
        raw = (building_info.properties or {}).get(field)
        out[name] = (
            _authoritative(
                raw,
                cap_key=cap_key,
                method="authoritative_building_info_point_join",
                rationale=(
                    "from the GEBAEUDEINFOOGD point covered by the authoritative roof outline"
                    + (
                        ", chosen as the nearest of several candidate points to the roof "
                        "centroid in the metric CRS"
                        if building_info.candidate_count > 1
                        else ""
                    )
                ),
                source=info_source,
                source_detail={
                    "layer": info_layer,
                    "field": field,
                    "raw_value": raw,
                    "OBJECTID": building_info.chosen_objectid,
                    **info_join_detail,
                },
                limitations=(
                    "GEBAEUDEINFOOGD covers historic stock; most of this study area is "
                    "post-2010 and carries no record",
                    *(
                        (
                            "several addressed buildings share this roof outline; the value "
                            "comes from the deterministically chosen nearest point, and the "
                            "competing candidates are recorded in source_detail",
                        )
                        if building_info.candidate_count > 1
                        else ()
                    ),
                ),
                cfg=cfg,
            )
            if raw is not None
            else _absent(
                info_layer,
                field,
                (
                    "no GEBAEUDEINFOOGD point falls on this roof outline (boundary included)"
                    if building_info.properties is None
                    else f"the GEBAEUDEINFOOGD point on this outline carries no {field}"
                ),
                info_source,
                dict(info_join_detail),
            )
        )

    _route_marginal_evidence(out, cfg)
    return out


def _quality_of(attr: Attribute) -> dict[str, Any]:
    return (attr.image_evidence or {}).get("quality") or {}


def _route_marginal_evidence(out: dict[str, Attribute], cfg: Config) -> None:
    """Attach ``requires_review`` to published values whose own evidence is thin (RTI-004).

    Machine-readable review routing at the attribute level: the value is published unchanged —
    it cleared every gate — but its deciding statistic sits close enough to that gate that a
    human should look before relying on it. Thresholds live under ``review:`` in
    ``configs/pipeline.yaml`` with their rationale; each reason names the measured number and
    the threshold so the flag is reproducible from the record. ``roof_type`` under conflict is
    routed in :func:`build_roof_type` itself.
    """
    ridge = out["ridge_orientation_deg"]
    gate_margin = _quality_of(ridge).get("gate_margin")
    review_margin = float(cfg.threshold("review", "ridge_gate_margin_review"))
    if ridge.value is not None and gate_margin is not None and gate_margin < review_margin:
        out["ridge_orientation_deg"] = replace(
            ridge,
            requires_review=True,
            review_reasons=(
                f"the ridge detection stands, but its weakest quality gate was cleared by "
                f"only {float(gate_margin):.1%} of that gate's own threshold (review margin "
                f"{review_margin:.0%}; see image_evidence.quality.gates): a modest "
                f"remeasurement could have withheld it",
            ),
        )

    surface_attr = out["visual_surface_appearance"]
    margin = _quality_of(surface_attr).get("margin_over_runner_up")
    surface_review = float(cfg.threshold("review", "surface_margin_review"))
    if (
        surface_attr.value is not None
        and surface_attr.value != "mixed"  # a mixed verdict does not rest on the margin
        and margin is not None
        and margin < surface_review
    ):
        out["visual_surface_appearance"] = replace(
            surface_attr,
            requires_review=True,
            review_reasons=(
                f"the leading appearance signature beats the runner-up by only "
                f"{float(margin):.1%} of judgeable pixels, within one sensitivity band of the "
                f"abstention gate (review margin {surface_review:.0%}): publishable, but a "
                f"small perturbation of the class boundaries would materially narrow it",
            ),
        )

    # With the shipped config this cannot fire: image.green_roof.negative_min_judgeable_fraction
    # equals review.vegetation_judgeable_review, so a negative below the review threshold
    # abstains at the observation stage and never publishes. The routing stays as the guard for
    # a configuration where the two thresholds diverge — review flags must never be the
    # mechanism that withholds a value, and this one never was.
    green = out["green_roof"]
    vegetation_judgeable = _quality_of(green).get("vegetation_judgeable_fraction")
    vegetation_review = float(cfg.threshold("review", "vegetation_judgeable_review"))
    if (
        green.value == "not_detected"
        and vegetation_judgeable is not None
        and vegetation_judgeable < vegetation_review
    ):
        out["green_roof"] = replace(
            green,
            requires_review=True,
            review_reasons=(
                f"a vegetation negative published over a roof of which only "
                f"{float(vegetation_judgeable):.1%} is imaged and unshadowed (review "
                f"threshold {vegetation_review:.0%}): the negative cleared the abstention "
                f"gate, but rests on a materially reduced share of the roof",
            ),
        )


# --------------------------------------------------------------------------------------
# stage 8 - one building record
# --------------------------------------------------------------------------------------

#: The image-observation attributes counted by the ``withheld_attributes`` review trigger
#: (RTI-004): the ones whose abstention is an evidence outcome for this building.
#: ``solar_panels`` is excluded because its verdict is withheld by policy on every building
#: (a failed detector evaluation, not this building's imagery), and ``roof_subtype`` because
#: it is not implemented at all - counting either would add a constant to every building and
#: blunt the signal the trigger exists to carry.
IMAGE_OBSERVATION_ATTRIBUTES = (
    "visual_surface_appearance",
    "green_roof",
    "ridge_orientation_deg",
    "footprint_axis_orientation_deg",
)


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
    audit_annotations: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ImageCrop, CvDelineation]:
    """One published building record, plus the crop and delineation the overlay needs.

    ``write_overlays`` is threaded in only so that ``overlay`` names a file that exists. Under
    ``--no-overlays`` no PNG is written, so the field is ``null`` rather than a dangling path.

    ``audit_annotations`` is the loaded transcription of the model-assisted visual audit
    (:mod:`propx_roofs.audit`). When present it adds a per-building ``manual_review`` report
    block and ``visual_audit_questionable`` review flags — report and routing only; no value,
    geometry or confidence is reachable from it.
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
    observations, processing_errors = observe(crop, record, cfg)

    typology = match_typology(record.geometry, typology_features, cfg)
    info_point = match_info_point(record.geometry, info_features, cfg)
    attributes = build_attributes(
        record,
        observations,
        metric,
        typology,
        info_point,
        cfg,
        sources,
    )

    # Measured whole-roof judgeability for the low_judgeability trigger (RTI-004): the same
    # adaptive measurement every observation publishes, recomputed here so the review routing
    # still has it when an observation stage degraded and recorded nothing.
    _, image_quality = judgeable_mask(crop, cfg)

    withheld = tuple(
        name
        for name in IMAGE_OBSERVATION_ATTRIBUTES
        if attributes[name].value is None
        and attributes[name].availability is Availability.UNAVAILABLE
    )
    manual_review = (
        audit.manual_review_for(audit_annotations, building_id)
        if audit_annotations is not None
        else None
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
            # Flagged only when an ambiguous best candidate was ACCEPTED (above the floor):
            # below the floor everything is withheld, nothing questionable is published, and
            # the competing shares are already recorded in source_detail.
            ambiguous_enrichment=(
                (typology.ambiguity_reason,)
                if typology.ambiguous
                and not typology.below_floor
                and typology.properties is not None
                else ()
            ),
            multiple_point_candidates=info_point.ambiguity_reason,
            processing_errors=tuple(sorted(processing_errors.items())),
            image_quality={
                "judgeable_fraction": image_quality.get("judgeable_fraction"),
                "shadow_fraction": image_quality.get("shadow_fraction"),
            },
            # Only when a candidate exists: with no candidate there is no agreement to be weak.
            cv_agreement=(
                {"iou": cv.iou, "topology_mismatch": cv.topology_mismatch}
                if cv.polygon is not None
                else None
            ),
            withheld_attributes=withheld,
            visual_audit=(
                audit.building_annotations(audit_annotations, building_id)
                if audit_annotations is not None
                else ()
            ),
        ),
        "overlay": f"overlays/{building_id}.png" if write_overlays else None,
    }
    if manual_review is not None:
        building["manual_review"] = manual_review
    return building, crop, cv


# --------------------------------------------------------------------------------------
# stage 9 - overlays
# --------------------------------------------------------------------------------------

OVERLAY_AUTHORITATIVE_BGR = (0, 220, 255)  # amber
OVERLAY_CV_BGR = (255, 64, 200)  # magenta
# The accepted ridge-support segment, drawn only when a ridge is actually published. Green is
# used by neither polygon colour above, so the three layers stay distinguishable.
OVERLAY_RIDGE_BGR = (80, 230, 80)
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


def _green_roof_caption(green_roof: str | None) -> str:
    """Three states, not two. ``None`` is an abstention and must not read as a negative.

    The attribute value is now the three-state string domain ``"detected"`` /
    ``"not_detected"`` / ``None`` (RTI-009), and the caption renders each distinctly. An
    earlier caption tested truthiness, which collapsed the abstention into "not detected" — the
    exact conflation the abstention design exists to prevent.
    """
    if green_roof is None:
        return "unavailable / unclear (no usable RGB evidence)"
    if green_roof == "detected":
        return "detected (low confidence, dormant season)"
    return "not detected (low confidence, dormant season)"


def _accepted_ridge_segment(attributes: dict[str, Any]) -> list[list[float]] | None:
    """WGS84 endpoints of the accepted ridge-support segment, or ``None`` when no ridge is
    published. The endpoints are the detector's own accepted segment
    (``image_evidence.quality.accepted_segment``), never a line synthesised from the azimuth —
    if a published ridge somehow lacked them, nothing is drawn rather than something invented.
    """
    ridge = attributes.get("ridge_orientation_deg") or {}
    if ridge.get("value") is None:
        return None
    quality = ((ridge.get("image_evidence") or {}).get("quality")) or {}
    endpoints = (quality.get("accepted_segment") or {}).get("endpoints_lonlat")
    if not endpoints or len(endpoints) != 2:
        return None
    return endpoints


def render_overlay(
    building: dict[str, Any], crop: ImageCrop, cv_result: CvDelineation
) -> np.ndarray:
    """Preliminary per-building overlay for Phase 3 visual inspection.

    Authoritative outline in amber, CV candidate in magenta, both labelled. When a ridge is
    published, the detector's accepted ridge-support segment is drawn in green and labelled as
    observed evidence, so the reviewer verifies the line the detector actually accepted; a
    withheld ridge draws nothing. Deliberately plain: it exists so a human can see which
    outline is which and whether the candidate wandered, not to be a figure. The caption names
    the primary polygon so the image cannot be misread as the CV stage having produced the
    delineation.
    """
    canvas = cv2.cvtColor(np.ascontiguousarray(crop.rgb), cv2.COLOR_RGB2BGR)
    _draw_rings(canvas, crop, building["authoritative_roof_polygon"], OVERLAY_AUTHORITATIVE_BGR)
    if cv_result.polygon is not None:
        _draw_rings(canvas, crop, cv_result.polygon, OVERLAY_CV_BGR)

    attributes = building["attributes"]
    ridge_segment = _accepted_ridge_segment(attributes)
    if ridge_segment is not None:
        (col0, row0), (col1, row1) = (
            imaging.lonlat_to_pixel(crop, lon, lat) for lon, lat in ridge_segment
        )
        cv2.line(
            canvas,
            (int(round(col0)), int(round(row0))),
            (int(round(col1)), int(round(row1))),
            OVERLAY_RIDGE_BGR,
            2,
            cv2.LINE_AA,
        )

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
        # An unqualified "green_roof not detected" on a dormant-season image reads as a
        # finding, which is exactly what design section 5.2 says it is not, so the caption
        # stays hedged. "surface appearance" names the renamed attribute: it is an appearance,
        # never a material claim.
        f"surface appearance {value('visual_surface_appearance')}"
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
    if ridge_segment is not None:
        legend.append(
            ("observed ridge evidence (accepted ridge-support segment)", OVERLAY_RIDGE_BGR)
        )
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


#: Sentinel: resolve the audit-annotations file to the repo default when the caller says
#: nothing. Distinct from ``None``, which explicitly disables attaching annotations.
AUDIT_ANNOTATIONS_DEFAULT = object()


def run(
    cfg: Config | None = None,
    *,
    generated_at: datetime | date | str | None = None,
    output_dir: Path | str | None = None,
    write_overlays: bool = True,
    audit_annotations: Path | str | None | object = AUDIT_ANNOTATIONS_DEFAULT,
) -> dict[str, Any]:
    """Run the whole pipeline offline and return the validated output document.

    ``generated_at`` is the only **intentionally variable** field in the document, and it is a
    parameter so that it can be pinned and two runs compared byte for byte. Pinning it does not
    make the comparison a reproducibility guarantee: source code, dependency binaries, the
    interpreter/runtime and the numerical libraries underneath (GEOS, OpenCV, NumPy) can all
    still affect byte-level output, and none of them is captured by the run fingerprints.

    When ``output_dir`` is given, the validated document, the per-building overlays and an
    ``artifact_manifest.json`` fingerprinting both are written there — **after** validation
    and atomically (see :func:`write_outputs`), so a failing run never leaves a document
    behind that looks like a result, and never a mixed old/new state.

    ``audit_annotations`` (RTI-004): the machine-readable transcription of the model-assisted
    visual audit. By default the repo's ``validation/audit_annotations.json`` is attached when
    it exists; pass a path to use another file, or ``None`` to attach nothing. Attached means
    *reported*: per-building ``manual_review`` blocks, ``visual_audit_questionable`` review
    flags, and the run-level ``manual_review`` status — the annotations cannot reach any
    value, geometry or confidence, and they describe an **earlier baseline** artifact whose
    identity travels with them in ``audit_basis``.
    """
    cfg = cfg or load_config()
    require_verified_cache(cfg)

    if audit_annotations is AUDIT_ANNOTATIONS_DEFAULT:
        annotations_path = audit.default_annotations_path()
    else:
        annotations_path = audit_annotations  # None disables; anything else is a path
    try:
        annotations = (
            audit.load_annotations(annotations_path) if annotations_path is not None else None
        )
    except audit.AuditError as error:
        # Loud: half-applying a broken review file would be worse than refusing to run.
        raise PipelineError(str(error)) from error

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
            audit_annotations=annotations,
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

    run_block = provenance.run_block(cfg, generated_at or datetime.now().astimezone())
    if annotations is not None:
        # A report of what is attached, not a claim of review: the status says QA of an
        # earlier baseline travels with this document, and audit_basis says which baseline.
        run_block["manual_review"] = audit.run_manual_review(annotations)
    document = {"run": run_block, "buildings": buildings}
    validate_output(document)

    if output_dir is not None:
        write_outputs(document, overlays, output_dir)
    return document


RUN_ARTIFACT_JSON = "roof_attributes.json"
RUN_OVERLAY_DIR = "overlays"
RUN_ARTIFACT_MANIFEST = "artifact_manifest.json"
#: The entries of the output directory that belong to a pipeline run. The swap in
#: :func:`write_outputs` touches exactly these three names and nothing else, so unrelated
#: pre-existing entries (e.g. the committed ``outputs/_solar_eval/``) survive every run.
RUN_ARTIFACT_NAMES = (RUN_ARTIFACT_JSON, RUN_OVERLAY_DIR, RUN_ARTIFACT_MANIFEST)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_staged(staging: Path, document: dict[str, Any]) -> list[str]:
    """Relative paths of the staged run files, after checking everything expected is present.

    The expectation comes from the document itself: every building whose ``overlay`` field
    names a PNG must have that PNG staged and non-empty, and a document with no overlays must
    stage no ``overlays/`` directory at all — so the final directory can never end up with a
    JSON that promises files the run did not produce, or with stale PNGs a previous run left.
    """
    json_path = staging / RUN_ARTIFACT_JSON
    if not json_path.is_file() or json_path.stat().st_size == 0:
        raise PipelineError(f"staged output is incomplete: missing {RUN_ARTIFACT_JSON}")
    expected = [b["overlay"] for b in document["buildings"] if b.get("overlay")]
    for relative in expected:
        staged = staging / relative
        if not staged.is_file() or staged.stat().st_size == 0:
            raise PipelineError(
                f"staged output is incomplete: the document names {relative} but no such "
                f"overlay was staged; refusing to publish a mixed state"
            )
    staged_overlays = (
        sorted(p.relative_to(staging).as_posix() for p in (staging / RUN_OVERLAY_DIR).rglob("*"))
        if (staging / RUN_OVERLAY_DIR).is_dir()
        else []
    )
    if sorted(expected) != staged_overlays:
        raise PipelineError(
            f"staged overlays {staged_overlays} do not match the overlays the document names "
            f"{sorted(expected)}; refusing to publish files the document does not describe"
        )
    if not expected and (staging / RUN_OVERLAY_DIR).exists():
        raise PipelineError(
            "the document names no overlays but an overlays directory was staged"
        )
    return [RUN_ARTIFACT_JSON, *sorted(expected)]


def build_artifact_manifest(staging: Path, document: dict[str, Any]) -> dict[str, Any]:
    """``artifact_manifest.json``: filename to sha256 for the JSON and every overlay.

    ``generated_at`` is deliberately taken **from the document**, not from the clock, so a
    run with a pinned ``generated_at`` produces a byte-identical manifest and the
    byte-reproduction checks (``make verify-repro``, ``scripts/finalize.sh``) keep working
    with the manifest present.
    """
    return {
        "generated_at": document["run"]["generated_at"],
        "config_hash": document["run"]["config_hash"],
        "note": (
            "sha256 fingerprints of this run's artifacts, written in the same atomic swap "
            "that published them. The manifest does not hash itself."
        ),
        "files": {name: _sha256_file(staging / name) for name in _verify_staged(staging, document)},
    }


def write_outputs(
    document: dict[str, Any], overlays: dict[str, np.ndarray], output_dir: Path | str
) -> Path:
    """Write the validated document, overlays and artifact manifest — atomically (RTI-015).

    The whole run is first written into a temporary sibling directory on the same filesystem
    (``<output_dir>.tmp-*``), verified complete against the document, and fingerprinted into
    ``artifact_manifest.json``. Only then is it moved into place, at the granularity of the
    run's own artifacts (:data:`RUN_ARTIFACT_NAMES`): the previous ``overlays/`` directory is
    renamed away before the new one is renamed in, and the JSON and manifest are installed
    with ``os.replace``. Anything else in the output directory is not touched.

    The precise guarantee: every individual move is an atomic POSIX same-filesystem rename,
    so the final directory **never contains a partially written or mixed artifact** — each of
    the three run artifacts is either the previous run's complete version or this run's
    complete version. The swap as a whole is not one atomic operation: a crash in the small
    window between renames can leave an artifact temporarily missing from the final
    directory, with its complete copy in a clearly named ``<output_dir>.tmp-*`` or
    ``<output_dir>.old-*`` sibling — recoverable by inspection, never a silent half-state.
    On any failure *before* the swap begins, the previous output state is untouched.

    Stale-state prevention: the final directory holds only this run's outputs under the three
    artifact names — an ``overlays/`` directory from a previous run is removed even when this
    run wrote fewer overlays or none at all (``--no-overlays``).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    staging = Path(
        tempfile.mkdtemp(dir=output_dir.parent, prefix=f"{output_dir.name}.tmp-")
    )
    try:
        (staging / RUN_ARTIFACT_JSON).write_text(
            json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        if overlays:
            overlay_dir = staging / RUN_OVERLAY_DIR
            overlay_dir.mkdir()
            for building_id, image in overlays.items():
                overlay_path = overlay_dir / f"{building_id}.png"
                # cv2.imwrite returns False on failure rather than raising, so an unwritable
                # path or a full disk would otherwise leave the CLI exiting 0 with overlays
                # missing - the exact "green run, invalid output" case the exit-code contract
                # exists to prevent.
                if not cv2.imwrite(str(overlay_path), image):
                    raise PipelineError(f"failed to write overlay {overlay_path}")
        manifest = build_artifact_manifest(staging, document)
        (staging / RUN_ARTIFACT_MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except BaseException:
        # Nothing was moved yet, so the previous output state is untouched; the staging
        # directory is removed so a failed run leaves no half-written tree behind.
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # The swap. Directories cannot be atomically replaced over a non-empty target, so the old
    # overlays are renamed away first; the two JSON files go in with os.replace, which never
    # leaves the name absent.
    old_overlays = output_dir / RUN_OVERLAY_DIR
    trash: Path | None = None
    if old_overlays.exists() or old_overlays.is_symlink():
        trash = Path(
            tempfile.mkdtemp(dir=output_dir.parent, prefix=f"{output_dir.name}.old-")
        )
        os.rename(old_overlays, trash / RUN_OVERLAY_DIR)
    staged_overlays = staging / RUN_OVERLAY_DIR
    if staged_overlays.exists():
        os.rename(staged_overlays, old_overlays)
    os.replace(staging / RUN_ARTIFACT_JSON, output_dir / RUN_ARTIFACT_JSON)
    os.replace(staging / RUN_ARTIFACT_MANIFEST, output_dir / RUN_ARTIFACT_MANIFEST)

    if trash is not None:
        shutil.rmtree(trash, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    return output_dir / RUN_ARTIFACT_JSON
