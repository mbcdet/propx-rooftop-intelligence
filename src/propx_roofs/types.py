"""Shared data contracts between pipeline stages.

These types are the interface between the geospatial stage, the image stage and the
provenance/confidence stage. They are deliberately plain frozen dataclasses: no behaviour
beyond serialisation, so no stage can smuggle logic into the contract.

Two invariants are enforced by construction rather than by convention:

* A metric value carries the CRS it was computed in. ``MetricGeometry`` can only be built
  from a projected transform, so a locally-scaled diagnostic geometry cannot be mistaken
  for a published area.
* ``Attribute.value is None`` requires an ``availability`` that explains why. Missing data
  is never zero and never invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Availability(str, Enum):
    """Why a value is what it is. Serialised as its string value."""

    OBSERVED = "observed"  # measured from imagery
    DERIVED = "derived"  # computed from geometry
    INFERRED = "inferred"  # classified, with uncertainty
    AUTHORITATIVE = "authoritative"  # taken from an official source
    UNAVAILABLE = "unavailable"  # not obtainable from any source used
    NOT_IN_SOURCE = "not_in_source"  # source consulted, this record absent

    def permits_null(self) -> bool:
        return self in (Availability.UNAVAILABLE, Availability.NOT_IN_SOURCE)


@dataclass(frozen=True)
class Confidence:
    """A documented heuristic reliability indicator. **Not a calibrated probability.**

    ``score`` is ``None`` when no value was produced. ``factors`` records every multiplicative
    penalty by name so a reader can reconstruct the score by hand.
    """

    method: str
    rationale: str
    sources: tuple[str, ...] = ()
    score: float | None = None
    limitations: tuple[str, ...] = ()
    factors: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.score is not None and not 0.0 <= self.score <= 1.0:
            raise ValueError(f"confidence score {self.score} outside [0, 1]")

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "method": self.method,
            "sources": list(self.sources),
            "rationale": self.rationale,
            "limitations": list(self.limitations),
            "factors": dict(self.factors),
            "note": "Documented heuristic reliability indicator, not a calibrated probability.",
        }


@dataclass(frozen=True)
class Attribute:
    """One published attribute with its provenance, evidence and confidence."""

    value: Any
    availability: Availability
    confidence: Confidence
    unit: str | None = None
    measure: str | None = None  # e.g. "planimetric"
    source_detail: dict[str, Any] | None = None  # layer / field / raw value
    image_evidence: dict[str, Any] | None = None  # independent image observation
    conflict: dict[str, Any] | None = None  # authoritative vs image disagreement

    def __post_init__(self) -> None:
        if self.value is None and not self.availability.permits_null():
            raise ValueError(
                f"null value requires availability unavailable/not_in_source, "
                f"got {self.availability.value}"
            )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "value": self.value,
            "availability": self.availability.value,
            "confidence": self.confidence.as_dict(),
        }
        for key, val in (
            ("unit", self.unit),
            ("measure", self.measure),
            ("source_detail", self.source_detail),
            ("image_evidence", self.image_evidence),
            ("conflict", self.conflict),
        ):
            if val is not None:
                out[key] = val
        return out


@dataclass(frozen=True)
class RoofRecord:
    """One authoritative ``ogdwien:ANLAGENLEISTUNG2025OGD`` feature, fields verbatim.

    ``geometry`` is a GeoJSON mapping in EPSG:4326. Source field names are preserved so any
    published value can be traced back to the dataset.
    """

    objectid: int
    geometry: dict[str, Any]
    dachform: str | None
    slope_mean: float | None
    anlagenleistung: float | None
    pot_ertrag_y_min: float | None
    pot_ertrag_y_max: float | None
    suitability_m2: dict[str, float | None]  # SCHLECHT / MITTEL / GUT / SEHRGUT
    adresse: str | None
    raw_properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricGeometry:
    """A geometry projected into a named projected CRS, with its derived measures.

    Construct only via ``propx_roofs.geometry.to_metric``. ``crs`` must be a projected CRS;
    the local cos(latitude) diagnostic frame is not permitted here.
    """

    crs: str
    area_m2: float
    n_polygons: int
    n_interior_rings: int
    interior_ring_area_m2: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "crs": self.crs,
            "area_m2": round(self.area_m2, 2),
            "n_polygons": self.n_polygons,
            "n_interior_rings": self.n_interior_rings,
            "interior_ring_area_m2": round(self.interior_ring_area_m2, 2),
        }


# Cardinality is always read from the BW_GEB_ID building unit TO the authoritative roof
# records, never the other way round. Published alongside every join so the notation cannot
# be misread: "1:n" means one building unit carries several roof records (the documented
# Seestadt cases 5729190 -> 4 and 5576077 -> 2), not several units under one record.
CARDINALITY_BASIS = "building_unit_to_roof_records"
CARDINALITIES = frozenset({"1:1", "1:n", "unmatched"})


@dataclass(frozen=True)
class JoinEvidence:
    """Result of matching an authoritative roof record to a dissolved FMZK unit.

    Cross-check only: the canonical published outline is always the roof record. ``iou`` and
    the containment ratios are comparison evidence, and every threshold behind ``ambiguous``
    is a documented heuristic.
    """

    matched_by: str  # always "best_geometric_overlap"
    # Read left-to-right as building unit -> authoritative roof records, per
    # CARDINALITY_BASIS. "n:1" is deliberately NOT a value: reversing the orientation is the
    # one mistake that silently inverts the meaning of every join in the output.
    cardinality: str  # "1:1" | "1:n" | "unmatched"
    bw_geb_id: int | None
    iou: float | None
    containment_roof_in_unit: float | None
    containment_unit_in_roof: float | None
    second_best_iou: float | None
    second_best_bw_geb_id: int | None
    ambiguous: bool
    ambiguity_reasons: tuple[str, ...]
    fmzk_parts_in_unit: int
    fmzk_parts_null_id_in_area: int
    fmzk_fetch_buffer_m: float
    unit_touches_fetch_boundary: bool
    competing_roof_record_ids: tuple[int, ...] = ()
    aggregation_applied: bool = False
    notes: tuple[str, ...] = ()
    cardinality_basis: str = CARDINALITY_BASIS

    def __post_init__(self) -> None:
        if self.cardinality not in CARDINALITIES:
            raise ValueError(
                f"cardinality {self.cardinality!r} not in {sorted(CARDINALITIES)}; "
                f"orientation is {CARDINALITY_BASIS}"
            )
        if self.cardinality == "1:n" and not self.competing_roof_record_ids:
            raise ValueError("1:n requires the competing roof record ids to be listed")
        if self.aggregation_applied:
            raise ValueError(
                "aggregation_applied must stay False: a genuine 1:n relationship is reported, "
                "never averaged or collapsed (design section 1.4 rule 5)"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched_by": self.matched_by,
            "cardinality": self.cardinality,
            "cardinality_basis": self.cardinality_basis,
            "bw_geb_id": self.bw_geb_id,
            "iou": self.iou,
            "containment_roof_in_unit": self.containment_roof_in_unit,
            "containment_unit_in_roof": self.containment_unit_in_roof,
            "second_best_iou": self.second_best_iou,
            "second_best_bw_geb_id": self.second_best_bw_geb_id,
            "ambiguous": self.ambiguous,
            "ambiguity_reasons": list(self.ambiguity_reasons),
            "fmzk_parts_in_unit": self.fmzk_parts_in_unit,
            "fmzk_parts_null_id_in_area": self.fmzk_parts_null_id_in_area,
            "fmzk_fetch_buffer_m": self.fmzk_fetch_buffer_m,
            "unit_touches_fetch_boundary": self.unit_touches_fetch_boundary,
            "competing_roof_record_ids": list(self.competing_roof_record_ids),
            "aggregation": {"applied": self.aggregation_applied},
            "notes": list(self.notes),
            "note": (
                "Cross-check only. The canonical published outline is the 2025 roof record. "
                "Thresholds are documented heuristics, not calibrated accuracy values."
            ),
        }


@dataclass(frozen=True)
class ImageCrop:
    """A per-building image window with the georeferencing needed to map back to WGS84.

    ``rgb`` is HxWx3 uint8. ``roof_mask`` is HxW bool, True inside the authoritative outline
    (interior rings excluded). ``origin_x``/``origin_y`` are EPSG:3857 metres at the
    upper-left corner of pixel (0, 0).
    """

    rgb: Any  # numpy.ndarray, HxWx3 uint8
    roof_mask: Any  # numpy.ndarray, HxW bool
    origin_x: float
    origin_y: float
    pixel_size_m: float
    zoom: int
    crs: str = "EPSG:3857"
    missing_tiles: tuple[str, ...] = ()


@dataclass(frozen=True)
class CvDelineation:
    """Image-derived candidate outline and its agreement with the authoritative outline.

    Never replaces the authoritative geometry. ``polygon`` is ``None`` when segmentation did
    not converge, which is a reportable outcome and not an error.
    """

    segmenter: str
    polygon: dict[str, Any] | None  # GeoJSON, EPSG:4326
    iou: float | None
    symmetric_difference_ratio: float | None
    hausdorff_m: float | None
    boundary_gradient_ratio: float | None
    alignment_warning: dict[str, Any] | None
    failure_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "segmenter": self.segmenter,
            "agreement_with_authoritative_geometry": {
                "iou": self.iou,
                "symmetric_difference_ratio": self.symmetric_difference_ratio,
                "hausdorff_m": self.hausdorff_m,
                "boundary_gradient_ratio": self.boundary_gradient_ratio,
            },
            "boundary_alignment_warning": self.alignment_warning,
            "failure_reason": self.failure_reason,
            "note": (
                "Comparison evidence only. The CV polygon is seeded from the authoritative "
                "outline, so this is agreement between related estimates, not segmentation "
                "accuracy. The primary outline is authoritative geodata, not a CV product."
            ),
        }


@dataclass(frozen=True)
class ImageObservation:
    """One image-derived observation, before confidence is assigned.

    ``value`` may be ``None`` to mean "could not determine" — the image stage abstains rather
    than guessing, and ``quality`` carries the reason.
    """

    name: str
    value: Any
    method: str
    rationale: str
    quality: dict[str, Any] = field(default_factory=dict)  # shadow fraction, margins, etc.
    limitations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "method": self.method,
            "rationale": self.rationale,
            "quality": dict(self.quality),
            "limitations": list(self.limitations),
        }
