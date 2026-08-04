"""Match an authoritative roof record to a dissolved FMZK unit by best geometric overlap.

Boundary identity was tested against real geometry and rejected: only 1 of 263 interior roof
records reaches IoU >= 0.99 against its ``BW_GEB_ID`` dissolve, while both containment ratios
sit symmetrically at ~0.98 — a genuine mutual re-cut of ~2% of area, not truncation
(design section 1.3, Amendment B1). So the join is best-IoU, and the evidence behind the
choice is published alongside it (section 1.4).

Three rules the code enforces rather than documents:

* **Every ratio is computed in EPSG:31256 via** ``geometry.to_metric``. The reconnaissance
  cos(latitude) frame is not used here even though IoU is a ratio, so that no published
  record contains a number whose provenance depends on which function happened to be called
  (Amendment B3).
* **Nothing is averaged, merged, or picked when the evidence is ambiguous.** A second unit
  competing for the same roof, or several roof records claiming the same unit, is reported as
  such. ``aggregation_applied`` stays ``False`` unless someone configures an explicit,
  reasoned aggregation — which this module does not do for them.
* **The thresholds are documented heuristics** read from ``configs/pipeline.yaml``. They were
  chosen to be conservative and are not calibrated accuracy values.

This is a **cross-check**. The canonical published outline is always the roof record
(section 1.4); nothing here can change it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from . import geometry
from .config import Config
from .types import JoinEvidence
from .units import DissolvedUnit

logger = logging.getLogger(__name__)

MATCHED_BY = "best_geometric_overlap"

_CROSSCHECK_NOTE = (
    "FMZK dissolve is a cross-check and a fallback; the canonical outline is the 2025 roof "
    "record."
)


@dataclass(frozen=True)
class _Candidate:
    """One intersecting unit and its overlap measures, all in the projected CRS."""

    unit: DissolvedUnit
    iou: float
    containment_roof_in_unit: float
    containment_unit_in_roof: float


def _candidates(
    roof_geom_wgs84: Any, units: Mapping[int, DissolvedUnit], crs: str
) -> tuple[list[_Candidate], bool]:
    """Overlap measures for every unit intersecting the roof, best IoU first.

    Returns ``(candidates, roof_has_area)``. The intersection test is done in WGS84 because
    it is topological and cheap; only the survivors pay for a projection.
    """
    roof_wgs84 = geometry.as_shapely(roof_geom_wgs84)
    roof_metric, roof_measures = geometry.to_metric(roof_wgs84, crs)
    if roof_measures.area_m2 <= 0:
        return [], False

    found: list[_Candidate] = []
    for unit in units.values():
        if not roof_wgs84.intersects(unit.geometry):
            continue
        unit_metric, unit_measures = geometry.to_metric(unit.geometry, crs)
        overlap = roof_metric.intersection(unit_metric).area
        if overlap <= 0 or unit_measures.area_m2 <= 0:
            continue
        union = roof_measures.area_m2 + unit_measures.area_m2 - overlap
        found.append(
            _Candidate(
                unit=unit,
                iou=overlap / union,
                containment_roof_in_unit=overlap / roof_measures.area_m2,
                containment_unit_in_roof=overlap / unit_measures.area_m2,
            )
        )
    # bw_geb_id breaks IoU ties so the same input always yields the same winner.
    found.sort(key=lambda c: (-c.iou, c.unit.bw_geb_id))
    return found, True


def match_roof_record(
    roof_geom_wgs84: Any,
    units: Mapping[int, DissolvedUnit],
    cfg: Config,
    *,
    fmzk_parts_null_id_in_area: int,
) -> JoinEvidence:
    """Best-IoU match of one roof outline against the dissolved FMZK units.

    ``roof_geom_wgs84`` is a shapely geometry or a GeoJSON mapping in EPSG:4326.
    ``fmzk_parts_null_id_in_area`` is the count returned by ``units.dissolve_units`` and is
    required rather than defaulted: the contract publishes it, and a default of 0 would be an
    invented value claiming full FMZK coverage.

    ``cardinality`` is provisionally ``"1:1"`` for any match. Only
    :func:`assign_cardinality`, which sees all records at once, can tell that several roof
    records claim the same unit.
    """
    crs = cfg.threshold("crs", "metric")
    buffer_m = float(cfg.threshold("join", "fmzk_buffer_m"))
    min_iou = float(cfg.threshold("join", "min_iou"))
    ambiguity_margin = float(cfg.threshold("join", "ambiguity_margin"))

    found, roof_has_area = _candidates(roof_geom_wgs84, units, crs)

    if not found:
        reason = (
            f"no FMZK unit intersects this outline within the fetched area "
            f"(buffer {buffer_m:g} m)"
            if roof_has_area
            else f"roof outline has zero area in {crs}, so no overlap can be computed"
        )
        return JoinEvidence(
            matched_by=MATCHED_BY,
            cardinality="unmatched",
            bw_geb_id=None,
            iou=None,
            containment_roof_in_unit=None,
            containment_unit_in_roof=None,
            second_best_iou=None,
            second_best_bw_geb_id=None,
            # Unmatched is itself unresolved evidence, so it is flagged, not passed as clean.
            ambiguous=True,
            ambiguity_reasons=(reason,),
            fmzk_parts_in_unit=0,
            fmzk_parts_null_id_in_area=fmzk_parts_null_id_in_area,
            fmzk_fetch_buffer_m=buffer_m,
            unit_touches_fetch_boundary=False,
            notes=(
                "No FMZK cross-check available for this record; the authoritative roof "
                "outline stands on its own. " + _CROSSCHECK_NOTE,
            ),
        )

    best = found[0]
    runner_up = found[1] if len(found) > 1 else None

    reasons: list[str] = []
    if best.iou < min_iou:
        reasons.append(
            f"best IoU {best.iou:.3f} is below the configured minimum overlap "
            f"{min_iou:.2f} (documented heuristic, not a calibrated accuracy figure)"
        )
    if runner_up is not None and best.iou > 0:
        margin_ratio = runner_up.iou / best.iou
        if margin_ratio > ambiguity_margin:
            reasons.append(
                f"second-best unit {runner_up.unit.bw_geb_id} at IoU {runner_up.iou:.3f} is "
                f"{margin_ratio:.2f} of the best ({best.iou:.3f}), above the ambiguity "
                f"margin {ambiguity_margin:.2f}: two units compete for this roof"
            )

    notes = [_CROSSCHECK_NOTE]
    if best.unit.touches_fetch_boundary:
        notes.append(
            "The matched unit reaches the edge of the fetched area, so parts of it may not "
            "have been downloaded; a depressed IoU here may reflect the query, not the data."
        )
    spread = abs(best.containment_roof_in_unit - best.containment_unit_in_roof)
    notes.append(
        f"Containment ratios differ by {spread:.3f}: "
        + (
            "roughly symmetric, consistent with a mutual re-cut of the outline rather than "
            "truncation."
            if spread < 0.05
            else "asymmetric, which is the signature of one geometry being truncated rather "
            "than re-cut. Worth inspecting."
        )
    )

    return JoinEvidence(
        matched_by=MATCHED_BY,
        cardinality="1:1",
        bw_geb_id=best.unit.bw_geb_id,
        iou=best.iou,
        containment_roof_in_unit=best.containment_roof_in_unit,
        containment_unit_in_roof=best.containment_unit_in_roof,
        second_best_iou=runner_up.iou if runner_up else None,
        second_best_bw_geb_id=runner_up.unit.bw_geb_id if runner_up else None,
        ambiguous=bool(reasons),
        ambiguity_reasons=tuple(reasons),
        fmzk_parts_in_unit=best.unit.part_count,
        fmzk_parts_null_id_in_area=fmzk_parts_null_id_in_area,
        fmzk_fetch_buffer_m=buffer_m,
        unit_touches_fetch_boundary=best.unit.touches_fetch_boundary,
        notes=tuple(notes),
    )


def assign_cardinality(
    evidences: Mapping[int, JoinEvidence],
) -> dict[int, JoinEvidence]:
    """Resolve cardinality across all roof records, preserving genuine 1:n relationships.

    Cardinality is read from the building unit TO the roof records
    (``types.CARDINALITY_BASIS``), so one unit carrying several roof records is **1:n**.
    The reverse notation would invert the meaning of every join in the output, which is why
    the orientation is published beside the value rather than left to the reader.

    ``evidences`` maps roof-record ``OBJECTID`` to the evidence from
    :func:`match_roof_record`. Where several roof records match the same ``BW_GEB_ID``, each
    of those records is marked ``cardinality="1:n"`` and lists the *other* competing record
    ids. Reconnaissance found real cases — Seestadt units 5729190 (4 roof records) and
    5576077 (2) — so this path exists and is tested even though the pinned sample is all 1:1.

    Nothing is averaged, dissolved, or resolved to a winner: the city's cadastre and its solar
    cadastre genuinely disagree about where one building ends, and picking one would publish a
    guess as a fact. ``aggregation_applied`` stays ``False``; an aggregation would need an
    explicit configured method and a concrete reason (design section 1.4, item 5).

    Such records are also flagged ``ambiguous``, because a cross-check that cannot say which
    building the roof belongs to has not verified anything. The threshold-based reasons from
    :func:`match_roof_record` are preserved and the cardinality reason is appended.
    """
    claimants: dict[int, list[int]] = {}
    for record_id, evidence in evidences.items():
        if evidence.bw_geb_id is not None:
            claimants.setdefault(evidence.bw_geb_id, []).append(record_id)

    resolved: dict[int, JoinEvidence] = {}
    for record_id, evidence in evidences.items():
        competitors = claimants.get(evidence.bw_geb_id or -1, [])
        if len(competitors) < 2:
            resolved[record_id] = evidence
            continue
        others = tuple(sorted(rid for rid in competitors if rid != record_id))
        # The building unit is NOT in doubt here - every competitor resolved to the same
        # BW_GEB_ID. What is unresolved is that several authoritative roof records describe
        # that one unit, so there is no single per-building value to publish without
        # inventing one.
        reason = (
            f"{len(competitors)} authoritative roof records "
            f"({', '.join(str(r) for r in sorted(competitors))}) describe the same building "
            f"unit {evidence.bw_geb_id}; they are reported separately because collapsing them "
            f"into one building value would invent a figure the sources do not agree on"
        )
        resolved[record_id] = replace(
            evidence,
            cardinality="1:n",
            competing_roof_record_ids=others,
            ambiguous=True,
            ambiguity_reasons=evidence.ambiguity_reasons + (reason,),
            aggregation_applied=False,
            notes=evidence.notes
            + (
                "Genuine 1:n relationship (one building unit, several roof records) preserved: "
                "every competing record is listed and none was averaged, merged, or chosen.",
            ),
        )
    n_multi = sum(1 for e in resolved.values() if e.cardinality == "1:n")
    if n_multi:
        logger.info("%d roof record(s) share a BW_GEB_ID unit; kept as 1:n", n_multi)
    return resolved
