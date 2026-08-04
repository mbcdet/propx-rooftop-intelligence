"""The schema has to reject the documents the design forbids, not merely accept good ones.

So each failure case below is a rule from the design that a plausible bug could break:
publishing the CV polygon as the primary outline (section 4), inventing an availability word
outside the section 5 vocabulary, dropping the "not a calibrated probability" note (section 6),
and shipping a value with no provenance at all.

The valid document is assembled through the real contracts — ``types``, ``confidence`` and
``provenance`` — rather than typed out as literal JSON, so a drift between the code and the
schema fails here instead of at the end of a pipeline run.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from propx_roofs import confidence, config, provenance, schema
from propx_roofs.types import (
    Attribute,
    Availability,
    CvDelineation,
    ImageObservation,
    JoinEvidence,
)

ROOF_LAYER = "PV-Potenzial 2025 (ogdwien:ANLAGENLEISTUNG2025OGD)"

POLYGON: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [
        [
            [16.37810, 48.18510],
            [16.37850, 48.18510],
            [16.37850, 48.18540],
            [16.37810, 48.18540],
            [16.37810, 48.18510],
        ]
    ],
    "crs": "EPSG:4326",
}


@pytest.fixture(scope="module")
def cfg() -> config.Config:
    return config.load()


def _authoritative(
    value: Any,
    *,
    field: str,
    cfg: config.Config,
    source_detail: dict[str, Any] | None = None,
    **extra: Any,
) -> Attribute:
    """One authoritative attribute, scored through the real confidence builder."""
    return Attribute(
        value=value,
        availability=Availability.AUTHORITATIVE,
        confidence=confidence.build(
            Availability.AUTHORITATIVE,
            "authoritative_single_record",
            "taken directly from the single matched roof record",
            (ROOF_LAYER,),
            {"source_recency": confidence.penalty("source_recency", cfg=cfg)},
            ("mean over the whole roof, not per plane",),
            # Section 5 caps mean_slope_deg at 0.90, and pipeline.yaml now configures it, so
            # the cap is read from config rather than written as a literal here.
            confidence.positive_cap("mean_slope_deg", cfg=cfg),
            cfg=cfg,
        ),
        source_detail=source_detail
        or {"layer": "ogdwien:ANLAGENLEISTUNG2025OGD", "field": field},
        **extra,
    )


def _building(cfg: config.Config) -> dict[str, Any]:
    delineation = CvDelineation(
        segmenter="grabcut+geometric_refinement",
        polygon=POLYGON,
        iou=0.87,
        symmetric_difference_ratio=0.14,
        hausdorff_m=1.4,
        boundary_gradient_ratio=0.92,
        alignment_warning={"flag": False, "metrics": None, "note": None},
    ).as_dict()
    delineation["primary_polygon"] = "authoritative_roof_polygon"

    join = JoinEvidence(
        matched_by="best_geometric_overlap",
        cardinality="1:1",
        bw_geb_id=5554877,
        iou=0.973,
        containment_roof_in_unit=0.983,
        containment_unit_in_roof=0.981,
        second_best_iou=0.041,
        second_best_bw_geb_id=5554878,
        ambiguous=False,
        ambiguity_reasons=(),
        fmzk_parts_in_unit=10,
        fmzk_parts_null_id_in_area=105,
        fmzk_fetch_buffer_m=cfg.threshold("join", "fmzk_buffer_m"),
        unit_touches_fetch_boundary=False,
    )

    area = Attribute(
        value=544.2,
        availability=Availability.DERIVED,
        confidence=confidence.build(
            Availability.DERIVED,
            "authoritative_outline_area_epsg31256",
            "planimetric area of the authoritative outline in EPSG:31256, courtyard void excluded",
            (ROOF_LAYER,),
            {},
            ("planimetric, not roof-surface area",),
            cfg=cfg,
        ),
        unit="m2",
        measure="planimetric",
    )

    return {
        "building_id": "vie-swv-005",
        "source_ids": {
            "anlagenleistung2025_objectid": 346520,
            "bw_geb_id": 5554877,
            "fmzk_ids": [4000837541, 4000837566],
        },
        "authoritative_roof_polygon": {
            **POLYGON,
            "polygon_source": "ogdwien:ANLAGENLEISTUNG2025OGD",
        },
        "cv_candidate_polygon": POLYGON,
        "delineation": delineation,
        "fmzk_crosscheck": join.as_dict(),
        "attributes": {
            "roof_area_m2": area.as_dict(),
            "roof_type": _authoritative(
                "flat",
                field="DACHFORM",
                cfg=cfg,
                source_detail={
                    "layer": "ogdwien:ANLAGENLEISTUNG2025OGD",
                    "field": "DACHFORM",
                    "raw_value": "Flachdach",
                },
            ).as_dict(),
            "mean_slope_deg": _authoritative(4.33, field="SLOPE_MEAN", cfg=cfg).as_dict(),
        },
        "review_flags": [],
    }


@pytest.fixture()
def document(cfg: config.Config) -> dict[str, Any]:
    return {
        "run": provenance.run_block(cfg, "2026-08-03T12:00:00+00:00"),
        "buildings": [_building(cfg)],
    }


# --- 7. the schema itself, and a document that must validate ---------------------------------


def test_schema_file_is_a_valid_draft_2020_12_schema() -> None:
    raw = json.loads(schema.SCHEMA_PATH.read_text(encoding="utf-8"))
    assert raw["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(raw)


def test_a_minimal_document_validates(document: dict[str, Any]) -> None:
    schema.validate(document)


def test_a_failed_segmentation_is_not_a_schema_error(document: dict[str, Any]) -> None:
    """``cv_candidate_polygon: null`` is a reportable outcome (section 4), so it must validate."""
    document["buildings"][0]["cv_candidate_polygon"] = None
    document["buildings"][0]["delineation"]["failure_reason"] = "grabcut did not converge"
    schema.validate(document)


def test_an_abstained_attribute_validates_with_a_null_score(
    cfg: config.Config, document: dict[str, Any]
) -> None:
    abstained = confidence.from_image_observation(
        ImageObservation(
            name="green_roof",
            value=None,
            method="rgb_vegetation_index_exg",
            rationale="roof interior too dark to judge",
            quality={"shadow_fraction": 0.62},
        ),
        "green_roof",
        cfg=cfg,
    )
    document["buildings"][0]["attributes"]["green_roof"] = abstained.as_dict()
    schema.validate(document)


def test_the_availability_vocabulary_is_exactly_the_design_list() -> None:
    enum = schema.load_schema()["$defs"]["availability"]["enum"]
    assert enum == [
        "observed",
        "derived",
        "inferred",
        "authoritative",
        "unavailable",
        "not_in_source",
    ]
    # The schema list and the Python enum must not drift apart.
    assert set(enum) == {member.value for member in Availability}


def test_schema_constants_match_the_provenance_module() -> None:
    """The notes and the attribution are ``const`` in the schema; a copy-paste drift is a bug."""
    defs = schema.load_schema()
    run = defs["$defs"]["run"]["properties"]
    assert run["confidence_note"]["const"] == provenance.CONFIDENCE_NOTE
    assert run["sample_note"]["const"] == provenance.SAMPLE_NOTE
    source = defs["$defs"]["source"]["properties"]
    assert source["licence"]["const"] == config.LICENCE
    assert source["attribution"]["const"] == config.ATTRIBUTION


# --- 8. documents that must fail --------------------------------------------------------------


def _fails(document: dict[str, Any], *, contains: str) -> None:
    with pytest.raises(schema.SchemaValidationError) as excinfo:
        schema.validate(document)
    assert contains in str(excinfo.value)


def test_a_cv_polygon_cannot_be_published_as_the_primary_outline(
    document: dict[str, Any],
) -> None:
    """Design section 4: nothing in the output may imply the outline came from CV."""
    document["buildings"][0]["delineation"]["primary_polygon"] = "cv_candidate_polygon"
    _fails(document, contains="delineation.primary_polygon")


def test_an_invented_availability_fails(document: dict[str, Any]) -> None:
    document["buildings"][0]["attributes"]["roof_type"]["availability"] = "guessed"
    _fails(document, contains="attributes.roof_type.availability")


def test_a_confidence_without_the_note_fails(document: dict[str, Any]) -> None:
    del document["buildings"][0]["attributes"]["roof_type"]["confidence"]["note"]
    _fails(document, contains="'note' is a required property")


def test_an_attribute_without_an_availability_fails(document: dict[str, Any]) -> None:
    del document["buildings"][0]["attributes"]["mean_slope_deg"]["availability"]
    _fails(document, contains="'availability' is a required property")


def test_a_confidence_without_a_method_or_rationale_fails(document: dict[str, Any]) -> None:
    confidence_block = document["buildings"][0]["attributes"]["roof_type"]["confidence"]
    confidence_block["method"] = ""
    _fails(document, contains="confidence.method")

    del confidence_block["rationale"]
    _fails(document, contains="'rationale' is a required property")


def test_a_fourth_factor_fails_in_the_document_too(document: dict[str, Any]) -> None:
    """The three-factor limit is enforced in the schema as well as in confidence.py."""
    document["buildings"][0]["attributes"]["roof_type"]["confidence"]["factors"] = {
        "source_recency": 0.95,
        "image_conflict": 0.70,
        "join_ambiguous": 0.85,
        "shadow_heavy": 0.70,
    }
    _fails(document, contains="confidence.factors")


def test_a_factor_outside_the_range_fails_in_the_document_too(document: dict[str, Any]) -> None:
    document["buildings"][0]["attributes"]["roof_type"]["confidence"]["factors"] = {
        "invented": 0.2
    }
    _fails(document, contains="confidence.factors.invented")


def test_a_misspelled_attribute_name_fails(document: dict[str, Any]) -> None:
    attributes = document["buildings"][0]["attributes"]
    attributes["roof_typ"] = attributes["roof_type"]
    _fails(document, contains="roof_typ")


def test_a_source_without_licence_or_attribution_fails(document: dict[str, Any]) -> None:
    del document["run"]["sources"][0]["licence"]
    _fails(document, contains="'licence' is a required property")


def test_a_rewritten_attribution_string_fails(document: dict[str, Any]) -> None:
    """CC BY attribution is verbatim or it is not attribution."""
    document["run"]["sources"][0]["attribution"] = "Data: City of Vienna"
    _fails(document, contains="sources[0].attribution")


def test_a_web_mercator_metric_crs_fails(document: dict[str, Any]) -> None:
    """Amendment B3: EPSG:3857 areas are wrong by ~2.2x at Vienna's latitude."""
    document["run"]["study_area"]["crs_metric"] = "EPSG:3857"
    _fails(document, contains="study_area.crs_metric")


def test_a_missing_join_evidence_field_fails(document: dict[str, Any]) -> None:
    """The containment ratios are what distinguish a mutual re-cut from truncation (section 1.4)."""
    del document["buildings"][0]["fmzk_crosscheck"]["containment_unit_in_roof"]
    _fails(document, contains="'containment_unit_in_roof' is a required property")


def test_an_unknown_join_rule_fails(document: dict[str, Any]) -> None:
    document["buildings"][0]["fmzk_crosscheck"]["matched_by"] = "boundary_identity"
    _fails(document, contains="fmzk_crosscheck.matched_by")


def test_a_typo_at_the_top_level_fails(document: dict[str, Any]) -> None:
    document["buildingz"] = document.pop("buildings")
    _fails(document, contains="buildingz")


def test_the_error_message_names_every_broken_path(document: dict[str, Any]) -> None:
    """A schema failure is meant to be fixable from the message alone."""
    document["buildings"][0]["attributes"]["roof_type"]["availability"] = "guessed"
    del document["buildings"][0]["attributes"]["mean_slope_deg"]["availability"]
    with pytest.raises(schema.SchemaValidationError) as excinfo:
        schema.validate(document)
    message = str(excinfo.value)
    assert "2 schema violation(s)" in message
    assert "$.buildings[0].attributes.mean_slope_deg" in message
    assert "$.buildings[0].attributes.roof_type.availability" in message
