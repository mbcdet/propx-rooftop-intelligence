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


def test_green_roof_value_domain_is_three_state_strings_not_booleans(
    cfg: config.Config, document: dict[str, Any]
) -> None:
    """RTI-009: the schema forbids Boolean green_roof values, so absence of evidence can never
    again be published as evidence of absence via a bare ``false``."""
    attribute = document["buildings"][0]["attributes"]
    negative = confidence.from_image_observation(
        ImageObservation(
            name="green_roof",
            value="not_detected",
            method="rgb_vegetation_index_exg",
            rationale="ExG below threshold over a judgeable roof",
            quality={"shadow_fraction": 0.02},
        ),
        "green_roof",
        cfg=cfg,
    )
    attribute["green_roof"] = negative.as_dict()
    schema.validate(document)

    attribute["green_roof"]["value"] = "detected"
    schema.validate(document)

    attribute["green_roof"]["value"] = False
    _fails(document, contains="green_roof")


def test_visual_surface_appearance_replaces_roof_surface_class(
    cfg: config.Config, document: dict[str, Any]
) -> None:
    """RTI-017: the renamed attribute takes only appearance terms; the old name and the old
    material-flavoured classes are schema errors."""
    attribute = document["buildings"][0]["attributes"]
    appearance = confidence.from_image_observation(
        ImageObservation(
            name="visual_surface_appearance",
            value="gravel_or_bitumen_like",
            method="rgb_colour_and_texture_appearance_statistics",
            rationale="near-neutral rough signature on 82% of judgeable pixels",
            quality={"dominance_share": 0.82},
        ),
        "visual_surface_appearance",
        availability=Availability.INFERRED,
        cfg=cfg,
    )
    attribute["visual_surface_appearance"] = appearance.as_dict()
    schema.validate(document)

    attribute["visual_surface_appearance"]["value"] = "bitumen_gravel"  # pre-rename class
    _fails(document, contains="visual_surface_appearance")

    del attribute["visual_surface_appearance"]
    attribute["roof_surface_class"] = appearance.as_dict()  # pre-rename attribute name
    attribute["roof_surface_class"]["value"] = "gravel_or_bitumen_like"
    _fails(document, contains="roof_surface_class")


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
    """The notes are ``const`` in the schema; a copy-paste drift is a bug."""
    defs = schema.load_schema()
    run = defs["$defs"]["run"]["properties"]
    assert run["confidence_note"]["const"] == provenance.CONFIDENCE_NOTE
    assert run["sample_note"]["const"] == provenance.SAMPLE_NOTE


def test_source_licence_and_attribution_are_required_but_not_vienna_constants() -> None:
    """RTI-019: per-source terms are required non-empty strings, no longer a baked-in const.

    Today's sources are all City of Vienna CC BY 4.0 — the recorded values say so — but the
    contract must admit a future differently-licensed source without a schema change.
    """
    source = schema.load_schema()["$defs"]["source"]
    for field in ("licence", "attribution"):
        assert field in source["required"]
        assert source["properties"][field]["type"] == "string"
        assert source["properties"][field]["minLength"] == 1
        assert "const" not in source["properties"][field]
    assert source["properties"]["terms_url"]["type"] == "string"
    assert "terms_url" not in source["required"]


def test_a_source_with_different_terms_and_a_terms_url_validates(
    document: dict[str, Any],
) -> None:
    document["run"]["sources"][0]["licence"] = "CC BY-SA 4.0"
    document["run"]["sources"][0]["attribution"] = "Datenquelle: Beispielanbieter"
    document["run"]["sources"][0]["terms_url"] = "https://example.org/terms"
    schema.validate(document)


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


def test_an_empty_attribution_string_fails(document: dict[str, Any]) -> None:
    """Terms are per-source and free-form since RTI-019, but blank is not attribution."""
    document["run"]["sources"][0]["attribution"] = ""
    _fails(document, contains="sources[0].attribution")


def test_a_run_without_the_cache_block_fails(document: dict[str, Any]) -> None:
    """RTI-011: a run that does not say which cache it read is missing provenance."""
    del document["run"]["cache"]
    _fails(document, contains="'cache' is a required property")


def test_a_malformed_cache_manifest_hash_fails(document: dict[str, Any]) -> None:
    document["run"]["cache"]["manifest_sha256"] = "not-a-sha"
    _fails(document, contains="cache.manifest_sha256")


def test_an_absent_cache_manifest_is_an_honest_null_and_validates(
    document: dict[str, Any],
) -> None:
    document["run"]["cache"] = {
        "manifest_sha256": None,
        "verified": False,
        "note": "no cache manifest at /nowhere/manifest.json",
    }
    schema.validate(document)


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


# --- RTI-014: semantically invalid states the schema must reject ------------------------------


def test_schema_version_is_1_2_0() -> None:
    """The const is the single source of truth and provenance.schema_version() reads it from
    here. 1.2.0: slim embedded audit annotations (code/summary + detail_ref instead of
    duplicated verbatim notes) and the optional reaudit_* human-review record."""
    declared = schema.load_schema()["$defs"]["run"]["properties"]["schema_version"]["const"]
    assert declared == "1.2.0"
    assert provenance.schema_version() == "1.2.0"


def test_a_determined_availability_with_a_null_value_fails(document: dict[str, Any]) -> None:
    """types.Attribute already refuses this at construction; the schema now refuses it in a
    hand-edited document too."""
    attribute = document["buildings"][0]["attributes"]["mean_slope_deg"]
    assert attribute["availability"] == "authoritative"
    attribute["value"] = None
    _fails(document, contains="mean_slope_deg")


def test_a_non_null_value_with_a_null_score_fails(document: dict[str, Any]) -> None:
    document["buildings"][0]["attributes"]["mean_slope_deg"]["confidence"]["score"] = None
    _fails(document, contains="mean_slope_deg")


def test_an_absent_availability_with_a_value_or_score_fails(document: dict[str, Any]) -> None:
    attribute = document["buildings"][0]["attributes"]["mean_slope_deg"]
    attribute["availability"] = "not_in_source"
    # value non-null under an absent availability: invalid.
    _fails(document, contains="mean_slope_deg")
    attribute["value"] = None
    # score non-null under an absent availability: still invalid.
    _fails(document, contains="mean_slope_deg")
    attribute["confidence"]["score"] = None
    schema.validate(document)


def test_empty_polygon_coordinates_fail(document: dict[str, Any]) -> None:
    document["buildings"][0]["authoritative_roof_polygon"]["coordinates"] = []
    _fails(document, contains="authoritative_roof_polygon.coordinates")


def _own_cv_polygon(document: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of the CV polygon, installed in the document, safe to mutate.

    The module-level POLYGON literal is shared between fixtures; mutating it in place would
    corrupt every later test.
    """
    polygon = json.loads(json.dumps(POLYGON))
    document["buildings"][0]["cv_candidate_polygon"] = polygon
    return polygon


def test_a_ring_with_too_few_positions_fails(document: dict[str, Any]) -> None:
    _own_cv_polygon(document)["coordinates"] = [
        [[16.378, 48.185], [16.379, 48.185], [16.378, 48.185]]
    ]
    _fails(document, contains="cv_candidate_polygon")


def test_a_position_that_is_not_two_numbers_fails(document: dict[str, Any]) -> None:
    coordinates = _own_cv_polygon(document)["coordinates"]
    coordinates[0][1] = [16.379]  # one number is not a position
    _fails(document, contains="cv_candidate_polygon")
    coordinates[0][1] = [16.379, 48.185, 12.0]  # this contract is strictly 2D
    _fails(document, contains="cv_candidate_polygon")


def test_multipolygon_nesting_depth_is_enforced(document: dict[str, Any]) -> None:
    """Polygon-depth coordinates under type MultiPolygon must fail: the silent one-level
    nesting mistake is exactly what pinned depths exist to catch."""
    polygon = _own_cv_polygon(document)
    rings = polygon["coordinates"]
    polygon["type"] = "MultiPolygon"
    _fails(document, contains="cv_candidate_polygon")
    polygon["coordinates"] = [rings]  # correctly wrapped: one polygon of those rings
    schema.validate(document)


def test_a_geometry_without_a_crs_fails(document: dict[str, Any]) -> None:
    document["buildings"][0]["authoritative_roof_polygon"] = {
        key: value
        for key, value in document["buildings"][0]["authoritative_roof_polygon"].items()
        if key != "crs"
    }
    _fails(document, contains="crs")


def test_every_new_review_trigger_validates_and_an_unknown_one_fails(
    document: dict[str, Any], cfg: config.Config
) -> None:
    for trigger in (
        "low_judgeability",
        "weak_cv_agreement",
        "withheld_attributes",
        "visual_audit_questionable",
    ):
        document["buildings"][0]["review_flags"] = [
            {"status": "requires_visual_review", "trigger": trigger, "reason": "measured"}
        ]
        schema.validate(document)
    document["buildings"][0]["review_flags"] = [
        {"status": "requires_visual_review", "trigger": "vibes", "reason": "no"}
    ]
    _fails(document, contains="review_flags")


def test_requires_review_and_review_reasons_travel_together(document: dict[str, Any]) -> None:
    attribute = document["buildings"][0]["attributes"]["mean_slope_deg"]
    attribute["requires_review"] = True
    _fails(document, contains="mean_slope_deg")  # a bare flag is unauditable
    attribute["review_reasons"] = ["margin thin against its own gate"]
    schema.validate(document)
    del attribute["requires_review"]
    _fails(document, contains="mean_slope_deg")  # reasons without the flag are invisible
    del attribute["review_reasons"]
    attribute["requires_review"] = False  # emitted only when true
    _fails(document, contains="mean_slope_deg")


def _manual_review_block() -> dict[str, Any]:
    return {
        "status": "visual_qa_annotated",
        "annotations": [
            {
                "aspect": "roof_type",
                "status": "conflicts",
                "code": "roof_type.conflicts",
                "summary": "Visual reading is flat; the authoritative value is not overridden.",
            }
        ],
        "detail_ref": "validation/audit_annotations.json#vie-swv-008",
        "audit_basis": {
            "source_file": "validation/visual_audit.json",
            "audited_document_sha256": "a" * 64,
            "baseline_commit": "c86cbf4",
            "reaudit_of_artifact_sha256": None,
        },
    }


def test_a_building_manual_review_block_validates(document: dict[str, Any]) -> None:
    document["buildings"][0]["manual_review"] = _manual_review_block()
    schema.validate(document)


def test_manual_review_vocabularies_are_closed(document: dict[str, Any]) -> None:
    block = _manual_review_block()
    block["annotations"][0]["status"] = "definitely_wrong"  # not an audit vocabulary word
    document["buildings"][0]["manual_review"] = block
    _fails(document, contains="manual_review")

    block = _manual_review_block()
    block["status"] = "reviewed"  # a building block may not claim human review
    document["buildings"][0]["manual_review"] = block
    _fails(document, contains="manual_review")

    block = _manual_review_block()
    del block["audit_basis"]  # annotations without their baseline are unattributable
    document["buildings"][0]["manual_review"] = block
    _fails(document, contains="manual_review")


def test_embedded_annotations_are_slim_by_contract(document: dict[str, Any]) -> None:
    """The document embeds aspect/status/code/summary and a detail_ref pointer only; a
    verbatim note in the artifact would reintroduce the duplication the slimming removed,
    and an unbounded summary would just be a note under a different name."""
    block = _manual_review_block()
    block["annotations"][0]["note"] = "the full verbatim paragraph"  # lives in validation/
    document["buildings"][0]["manual_review"] = block
    _fails(document, contains="manual_review")

    block = _manual_review_block()
    del block["annotations"][0]["summary"]
    document["buildings"][0]["manual_review"] = block
    _fails(document, contains="manual_review")

    block = _manual_review_block()
    block["annotations"][0]["summary"] = "x" * 301  # over the embedded cap
    document["buildings"][0]["manual_review"] = block
    _fails(document, contains="manual_review")

    block = _manual_review_block()
    del block["detail_ref"]  # the pointer to the full trail is part of the contract
    document["buildings"][0]["manual_review"] = block
    _fails(document, contains="manual_review")

    block = _manual_review_block()
    block["detail_ref"] = "somewhere/else.json#vie-swv-008"  # the trail has one home
    document["buildings"][0]["manual_review"] = block
    _fails(document, contains="manual_review")


def test_audit_basis_accepts_a_recorded_human_review_and_rejects_a_malformed_one(
    document: dict[str, Any],
) -> None:
    """The reaudit_* record written by record-review must validate; a non-sha256 overlay
    hash or an empty reviewer must not."""
    block = _manual_review_block()
    block["audit_basis"].update(
        {
            "reaudit_of_artifact_sha256": "b" * 64,
            "reaudit_overlays": {"overlays/vie-swv-001.png": "c" * 64},
            "reaudit_reviewer": "Mohammad",
            "reaudit_date": "2026-08-08",
        }
    )
    document["buildings"][0]["manual_review"] = block
    schema.validate(document)

    block["audit_basis"]["reaudit_overlays"]["overlays/vie-swv-001.png"] = "not-a-hash"
    _fails(document, contains="manual_review")

    block["audit_basis"]["reaudit_overlays"]["overlays/vie-swv-001.png"] = "c" * 64
    block["audit_basis"]["reaudit_reviewer"] = ""
    _fails(document, contains="manual_review")


def test_the_run_manual_review_accepts_the_attached_qa_status(
    document: dict[str, Any],
) -> None:
    document["run"]["manual_review"] = {
        "status": "visual_qa_of_baseline_attached",
        "reviewer": None,
        "n": 10,
        "audit_basis": _manual_review_block()["audit_basis"],
        "note": "model-assisted QA of an earlier baseline; not human review",
    }
    schema.validate(document)
    document["run"]["manual_review"]["status"] = "audited"  # not a defined status
    _fails(document, contains="manual_review")
