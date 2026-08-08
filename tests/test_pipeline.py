"""End-to-end behaviour of the wiring, asserted against the real committed cache.

These tests run the actual pipeline over the ten pinned buildings. That is slow enough to be
worth justifying: the properties being checked here — that the primary polygon is never a CV
product, that a missing source becomes ``not_in_source`` and not zero, that every published
metre came from EPSG:31256 — are properties of the *composition*, and a mocked run would assert
them about the mocks.

Two rules govern what is asserted about content:

* **Nothing is asserted into existence.** The vie-swv-008 test asserts what the implemented
  detector actually produces (since RTI-007's quality gates: a withheld ridge, so no image
  reading and no conflict), and explicitly asserts that no conflict is flagged. Writing a test
  that demanded a conflict there would have forced the detector to be retuned until it
  produced one.
* **The mechanisms that have no exemplar in the sample are tested synthetically** — the CV
  failure path and the authoritative-versus-image conflict — so their correctness never depends
  on a real building behaving a particular way.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from shapely.geometry import box

from propx_roofs import audit, config, geometry, pipeline, provenance, units
from propx_roofs.segment import agreement
from propx_roofs.types import CARDINALITY_BASIS, ImageObservation

GENERATED_AT_A = "2026-08-04T00:00:00+00:00"
GENERATED_AT_B = "2199-12-31T23:59:59+00:00"

# The ten pinned OBJECTIDs, written out rather than read from the config: a test that derives
# its expectation from the same file as the code cannot catch the file being misread.
PINNED_OBJECTIDS = [
    351705,
    358722,
    351704,
    308209,
    346520,
    242275,
    346007,
    358486,
    358487,
    257807,
]
PINNED_IDS = [f"vie-swv-{n:03d}" for n in range(1, 11)]

ENRICHMENT_ATTRIBUTES = (
    "construction_epoch",
    "building_typology",
    "year_built",
    "architect",
)


@pytest.fixture(scope="session")
def cfg():
    return config.load()


@pytest.fixture(scope="session")
def run_a(cfg, tmp_path_factory):
    """One real run, shared by every read-only test."""
    out = tmp_path_factory.mktemp("run_a")
    document = pipeline.run(cfg, generated_at=GENERATED_AT_A, output_dir=out)
    return document, out


@pytest.fixture(scope="session")
def document(run_a):
    return run_a[0]


@pytest.fixture(scope="session")
def buildings(document) -> dict[str, dict[str, Any]]:
    return {b["building_id"]: b for b in document["buildings"]}


# ---------------------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------------------


def test_exactly_the_ten_pinned_objectids_are_selected(cfg, document):
    selected = [b["source_ids"]["anlagenleistung2025_objectid"] for b in document["buildings"]]
    assert selected == PINNED_OBJECTIDS
    # And they are a strict subset of what the cache holds, so the cache is not the selector.
    cached = pipeline.load_roof_records(cfg)
    assert len(cached) > len(selected)
    assert set(selected) < set(cached)


def test_a_missing_pinned_record_is_fatal_rather_than_a_short_document(cfg):
    records = pipeline.load_roof_records(cfg)
    records.pop(PINNED_OBJECTIDS[0])
    with pytest.raises(pipeline.PipelineError, match="refusing to publish a partial one"):
        pipeline.select_pinned(records, cfg)


def test_output_has_exactly_ten_unique_building_ids(document):
    ids = [b["building_id"] for b in document["buildings"]]
    assert ids == PINNED_IDS
    assert len(set(ids)) == 10


# ---------------------------------------------------------------------------------------
# the cache gate
# ---------------------------------------------------------------------------------------


def test_the_real_cache_verifies(cfg):
    assert pipeline.verify_cache(cfg) == []


def test_cache_verification_failure_stops_before_any_building_work(cfg, monkeypatch):
    """A bad cache must not reach the building loop, not merely fail somewhere later."""
    touched: list[str] = []

    monkeypatch.setattr(pipeline, "verify_cache", lambda _cfg: ["synthetic cache problem"])
    for stage in ("load_roof_records", "join_units", "build_building"):
        monkeypatch.setattr(
            pipeline, stage, lambda *a, _s=stage, **k: touched.append(_s) or pytest.fail(_s)
        )

    with pytest.raises(pipeline.PipelineError) as raised:
        pipeline.run(cfg, generated_at=GENERATED_AT_A)

    assert "synthetic cache problem" in str(raised.value)
    assert "no building was processed" in str(raised.value)
    assert touched == []


# ---------------------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------------------


def test_real_output_validates_against_the_packaged_schema(document):
    pipeline.validate_output(document)  # raises on any violation


def test_the_schema_really_is_read_from_the_package(document):
    packaged = pipeline.packaged_schema()
    assert packaged["$id"].endswith("roof_attributes.schema.json")
    assert packaged["properties"]["buildings"]["items"]["$ref"] == "#/$defs/building"


def test_an_invalid_document_fails_validation(document):
    broken = json.loads(json.dumps(document))
    broken["buildings"][0]["delineation"]["primary_polygon"] = "cv_candidate_polygon"
    with pytest.raises(Exception, match="primary_polygon"):
        pipeline.validate_output(broken)


# ---------------------------------------------------------------------------------------
# delineation: authoritative primary, CV as evidence
# ---------------------------------------------------------------------------------------


def test_authoritative_polygon_is_primary_in_every_record(cfg, document):
    records = pipeline.load_roof_records(cfg)
    for building in document["buildings"]:
        source = records[building["source_ids"]["anlagenleistung2025_objectid"]]
        polygon = building["authoritative_roof_polygon"]
        assert building["delineation"]["primary_polygon"] == "authoritative_roof_polygon"
        assert polygon["polygon_source"] == "ogdwien:ANLAGENLEISTUNG2025OGD"
        # Byte-for-byte the source outline: nothing in the pipeline re-cuts it.
        assert polygon["coordinates"] == source.geometry["coordinates"]
        assert polygon["type"] == source.geometry["type"]
        # And it is never the CV candidate.
        assert building["cv_candidate_polygon"] != {
            "type": polygon["type"],
            "coordinates": polygon["coordinates"],
            "crs": polygon["crs"],
        }


def test_agreement_is_never_labelled_accuracy(document):
    text = json.dumps(document).lower()
    assert "not segmentation accuracy" in text
    assert "segmentation accuracy of" not in text
    for building in document["buildings"]:
        assert "agreement_with_authoritative_geometry" in building["delineation"]


def test_cv_failure_is_a_reportable_outcome(cfg, monkeypatch):
    """Forced synthetically: no selected building fails segmentation on the real cache."""
    monkeypatch.setattr(
        pipeline, "segment_roof", lambda crop, cfg: (None, "synthetic_forced_failure")
    )
    document = pipeline.run(cfg, generated_at=GENERATED_AT_A, write_overlays=False)

    assert len(document["buildings"]) == 10
    for building in document["buildings"]:
        assert building["cv_candidate_polygon"] is None
        delineation = building["delineation"]
        assert delineation["failure_reason"] == "synthetic_forced_failure"
        assert delineation["primary_polygon"] == "authoritative_roof_polygon"
        assert all(
            value is None
            for value in delineation["agreement_with_authoritative_geometry"].values()
        )
        # The authoritative outline is untouched, so the record loses nothing.
        assert building["authoritative_roof_polygon"]["coordinates"]
        assert building["attributes"]["roof_area_m2"]["value"] > 0

    pipeline.validate_output(document)  # a failed candidate is still a valid document


def test_a_segmenter_exception_is_reported_not_raised(cfg, monkeypatch):
    def explode(crop, cfg):
        raise RuntimeError("synthetic opencv failure")

    monkeypatch.setattr(pipeline.grabcut, "segment", explode)
    crop = object()
    assert pipeline.segment_roof(crop, cfg) == (None, "segmenter_raised_RuntimeError")


# ---------------------------------------------------------------------------------------
# CRS: every published metre from EPSG:31256
# ---------------------------------------------------------------------------------------


def test_published_areas_come_from_epsg31256_and_not_the_diagnostic_frame(cfg, document):
    """The two frames differ by ~0.743%; the published value must match the projected one."""
    records = pipeline.load_roof_records(cfg)
    lat0 = 0.5 * (cfg.study_area.bbox_wgs84[1] + cfg.study_area.bbox_wgs84[3])
    for building in document["buildings"]:
        record = records[building["source_ids"]["anlagenleistung2025_objectid"]]
        attribute = building["attributes"]["roof_area_m2"]
        _, projected = geometry.to_metric(record.geometry, "EPSG:31256")
        diagnostic = geometry.local_diagnostic_scale(record.geometry, lat0).area

        assert attribute["value"] == round(projected.area_m2, 2)
        assert attribute["source_detail"]["crs"] == "EPSG:31256"
        assert attribute["measure"] == "planimetric"
        # Not the recon frame: the offset is systematic and one-sided.
        relative = (projected.area_m2 - diagnostic) / projected.area_m2
        assert 0.005 < relative < 0.01
        assert attribute["value"] != round(diagnostic, 2)


def test_the_metric_crs_is_the_one_declared_everywhere(document):
    assert document["run"]["study_area"]["crs_metric"] == "EPSG:31256"
    for building in document["buildings"]:
        assert building["attributes"]["roof_area_m2"]["source_detail"]["crs"] == "EPSG:31256"


# ---------------------------------------------------------------------------------------
# the FMZK cross-check
# ---------------------------------------------------------------------------------------


def test_fmzk_provenance_records_the_150_m_buffer(cfg, document):
    configured = float(cfg.threshold("join", "fmzk_buffer_m"))
    assert configured == 150.0
    for building in document["buildings"]:
        assert building["fmzk_crosscheck"]["fmzk_fetch_buffer_m"] == configured

    # The published buffer is not a label: the parts really were fetched for the wider box.
    study = cfg.study_area.bbox_wgs84
    buffered = units.buffered_fetch_bbox(study, cfg)
    study_m, _ = geometry.to_metric(box(*study), "EPSG:31256")
    buffered_m, _ = geometry.to_metric(box(*buffered), "EPSG:31256")
    for a, b in zip(study_m.bounds[:2], buffered_m.bounds[:2], strict=True):
        assert a - b == pytest.approx(configured, abs=1.0)
    for a, b in zip(study_m.bounds[2:], buffered_m.bounds[2:], strict=True):
        assert b - a == pytest.approx(configured, abs=1.0)

    manifest = json.loads((cfg.study_area.cache_dir / "manifest.json").read_text())
    assert manifest["requests"]["fmzk_buffer_m"] == configured
    assert [round(v, 9) for v in manifest["requests"]["fmzk_requested_bbox_wgs84"]] == [
        round(v, 9) for v in buffered
    ]


def test_cardinality_orientation_is_building_unit_to_roof_records(document):
    for building in document["buildings"]:
        crosscheck = building["fmzk_crosscheck"]
        assert crosscheck["cardinality_basis"] == CARDINALITY_BASIS
        assert crosscheck["cardinality_basis"] == "building_unit_to_roof_records"
        assert crosscheck["cardinality"] in {"1:1", "1:n", "unmatched"}
        assert crosscheck["matched_by"] == "best_geometric_overlap"
        assert crosscheck["aggregation"]["applied"] is False
    # The reversed notation would invert the meaning of every join, so it may not appear at all.
    assert "n:1" not in json.dumps(document)


def test_the_join_is_recomputed_and_not_seeded_from_the_pinned_ids(cfg, document):
    """The config's bw_geb_id is a cross-check, not an input: the match must agree with it."""
    pinned = {b.building_id: b.bw_geb_id for b in cfg.study_area.buildings}
    for building in document["buildings"]:
        assert building["source_ids"]["bw_geb_id"] == pinned[building["building_id"]]
        assert building["fmzk_crosscheck"]["iou"] > 0.9


def test_cardinality_is_assigned_over_every_cached_record_not_only_the_sample(cfg):
    """A 1:n that a non-pinned record causes must still be visible on a pinned one."""
    records = pipeline.load_roof_records(cfg)
    evidences, _ = pipeline.join_units(records, cfg)
    assert set(evidences) == set(records)
    assert len(evidences) > 10


# ---------------------------------------------------------------------------------------
# absent data
# ---------------------------------------------------------------------------------------


def test_absent_enrichment_is_null_and_not_in_source_never_zero(buildings):
    seen_absent = seen_present = 0
    for building in buildings.values():
        for name in ENRICHMENT_ATTRIBUTES:
            attribute = building["attributes"][name]
            if attribute["availability"] == "not_in_source":
                assert attribute["value"] is None
                assert attribute["confidence"]["score"] is None
                seen_absent += 1
            else:
                assert attribute["availability"] == "authoritative"
                assert attribute["value"] not in (None, 0, "")
                seen_present += 1
    assert seen_absent > 0 and seen_present > 0


def test_the_sparse_authoritative_context_is_reported_as_it_is(buildings):
    """Design risk 2b, after RTI-012: only 1 of 10 keeps a typology, 1 of 10 has a BAUJAHR.

    vie-swv-007 (49.1%) and vie-swv-010 (44.8%) sat below the 50% minimum overlap and are now
    withheld rather than published with a discounted confidence.
    """
    with_typology = [
        bid
        for bid, b in buildings.items()
        if b["attributes"]["building_typology"]["availability"] == "authoritative"
    ]
    with_year = [
        bid
        for bid, b in buildings.items()
        if b["attributes"]["year_built"]["availability"] == "authoritative"
    ]
    assert with_typology == ["vie-swv-005"]
    assert with_year == ["vie-swv-009"]
    assert buildings["vie-swv-009"]["attributes"]["year_built"]["value"] == 1891


def test_typology_confidence_reflects_the_published_overlap(buildings):
    """The one surviving match keeps its measured share as its confidence factor."""
    attribute = buildings["vie-swv-005"]["attributes"]["building_typology"]
    assert attribute["availability"] == "authoritative"
    assert attribute["source_detail"]["overlap_fraction_of_roof"] == 0.6649
    assert attribute["source_detail"]["overlap_fraction_of_source_polygon"] == 0.9984
    assert attribute["confidence"]["factors"]["typology_overlap"] == 0.6649
    assert attribute["confidence"]["score"] == 0.600


def test_a_below_floor_typology_overlap_is_withheld_not_discounted(buildings):
    """RTI-012 on the real cache: 007 at 49.1% and 010 at 44.8% abstain below the 50% floor.

    The floor was chosen on the majority principle (the polygon must describe at least half
    the roof), not tuned to these buildings; both shares, the floor and every candidate stay
    recorded so the withholding is auditable. No review flag fires: nothing questionable was
    published, which is the point.
    """
    expected = {"vie-swv-007": (0.4910, 0.9813), "vie-swv-010": (0.4479, 0.9972)}
    for building_id, (share_roof, share_source) in expected.items():
        for name in ("building_typology", "construction_epoch"):
            attribute = buildings[building_id]["attributes"][name]
            assert attribute["value"] is None
            assert attribute["availability"] == "not_in_source"
            assert attribute["confidence"]["score"] is None
            assert "withheld" in attribute["confidence"]["rationale"]
            assert "50%" in attribute["confidence"]["rationale"]
            detail = attribute["source_detail"]
            assert detail["overlap_fraction_of_roof"] == share_roof
            assert detail["overlap_fraction_of_source_polygon"] == share_source
            assert detail["min_overlap_fraction_of_roof"] == 0.5
            assert detail["candidate_count"] > 1
            assert detail["candidates"][0]["overlap_fraction_of_roof"] == share_roof
            # Two similar candidates: the ambiguity is recorded, but with everything withheld
            # no ambiguous_enrichment review flag fires.
            assert detail["ambiguous"] is True
        triggers = {f["trigger"] for f in buildings[building_id]["review_flags"]}
        assert "ambiguous_enrichment" not in triggers
    # The raw value is preserved verbatim so the withholding is traceable, never promoted.
    detail = buildings["vie-swv-010"]["attributes"]["building_typology"]["source_detail"]
    assert detail["raw_value"] == "W4.1.-soz.u.gemeinn.Wohnb.-Baulückenbebauungen"


def test_the_point_join_decision_is_recorded_on_the_real_sample(buildings):
    """RTI-013: candidate_count is auditable even when it is 0 or 1, on every building."""
    expected_counts = {
        "vie-swv-005": 1,
        "vie-swv-007": 1,
        "vie-swv-008": 1,
        "vie-swv-009": 1,
        "vie-swv-010": 1,
    }
    for building_id, building in buildings.items():
        detail = building["attributes"]["year_built"]["source_detail"]
        assert detail["candidate_count"] == expected_counts.get(building_id, 0)
        assert detail["matched_by"] == "point_covered_by_authoritative_roof_polygon"
        assert detail["selection_rule"] == "nearest_to_roof_centroid_in_metric_crs"
        assert len(detail["candidates"]) == detail["candidate_count"]
        for candidate in detail["candidates"]:
            assert candidate["distance_to_roof_centroid_m"] >= 0


def test_a_present_record_with_a_null_field_is_still_not_in_source(buildings):
    """vie-swv-008 has a GEBAEUDEINFOOGD point, but that point carries no BAUJAHR."""
    attribute = buildings["vie-swv-008"]["attributes"]["year_built"]
    assert attribute["value"] is None
    assert attribute["availability"] == "not_in_source"
    assert "carries no BAUJAHR" in attribute["confidence"]["rationale"]


def test_an_abstaining_image_observation_publishes_no_score(buildings):
    """Where no ridge was detected the attribute is unavailable with a null score, not false."""
    absent = [
        b
        for b in buildings.values()
        if b["attributes"]["ridge_orientation_deg"]["value"] is None
    ]
    assert absent, "expected at least one roof with no detected ridge"
    for building in absent:
        attribute = building["attributes"]["ridge_orientation_deg"]
        assert attribute["availability"] == "unavailable"
        assert attribute["confidence"]["score"] is None
        assert "abstention" in " ".join(attribute["confidence"]["limitations"])


# ---------------------------------------------------------------------------------------
# stage 6 enrichment joins: the floor, the margin, and the point rule (RTI-012 / RTI-013)
# ---------------------------------------------------------------------------------------

# A building-sized patch of the study area for synthetic join geometry.
_LON0, _LON1, _LAT0, _LAT1 = 16.3790, 16.3800, 48.1850, 48.1860


def _polygon_feature(objectid: int, lon_lo: float, lon_hi: float, **props: Any) -> dict:
    return {
        "type": "Feature",
        "properties": {"OBJECTID": objectid, **props},
        "geometry": box(lon_lo, _LAT0, lon_hi, _LAT1).__geo_interface__,
    }


def _point_feature(objectid: int, lon: float, lat: float, **props: Any) -> dict:
    return {
        "type": "Feature",
        "properties": {"OBJECTID": objectid, **props},
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    }


def test_match_typology_measures_both_shares_and_counts_candidates(cfg):
    roof = box(_LON0, _LAT0, _LON1, _LAT1)
    # One polygon covering ~70% of the roof and extending beyond it, one ~10% sliver.
    features = [
        _polygon_feature(1, _LON0, _LON0 + 0.0007),
        _polygon_feature(2, _LON1 - 0.0001, _LON1 + 0.0004),
    ]
    match = pipeline.match_typology(roof, features, cfg)

    assert match.candidate_count == 2
    assert match.properties["OBJECTID"] == 1
    assert match.share_of_roof == pytest.approx(0.7, abs=0.01)
    # intersection over the SOURCE polygon: the winner lies entirely on the roof, the sliver
    # only 1/5 on it — the second number a reader needs to see a neighbour's polygon for what
    # it is.
    assert match.share_of_source == pytest.approx(1.0, abs=0.01)
    assert match.candidates[1]["overlap_fraction_of_source_polygon"] == pytest.approx(
        0.2, abs=0.01
    )
    assert match.below_floor is False
    assert match.ambiguous is False


def test_match_typology_below_the_floor_is_withheld_not_published(cfg):
    """A 30% overlap is more likely a neighbour's polygon than this roof's typology."""
    roof = box(_LON0, _LAT0, _LON1, _LAT1)
    features = [_polygon_feature(1, _LON0, _LON0 + 0.0003, BAUTYP_TXT="synthetic")]
    match = pipeline.match_typology(roof, features, cfg)

    assert match.share_of_roof == pytest.approx(0.3, abs=0.01)
    assert match.below_floor is True
    assert match.min_overlap == 0.5


def test_match_typology_flags_two_competing_candidates(cfg):
    """Two polygons each covering ~half the roof: the winner is not evidence of anything."""
    roof = box(_LON0, _LAT0, _LON1, _LAT1)
    features = [
        _polygon_feature(1, _LON0, _LON0 + 0.00055),
        _polygon_feature(2, _LON0 + 0.00055, _LON1),
    ]
    match = pipeline.match_typology(roof, features, cfg)

    assert match.candidate_count == 2
    assert match.below_floor is False
    assert match.ambiguous is True
    assert "compete for this roof" in match.ambiguity_reason
    assert match.ambiguity_margin == 0.70


def test_match_typology_with_no_overlap_reports_zero_candidates(cfg):
    match = pipeline.match_typology(box(_LON0, _LAT0, _LON1, _LAT1), [], cfg)
    assert match.properties is None
    assert match.candidate_count == 0
    assert match.candidates == ()
    assert match.below_floor is False and match.ambiguous is False


def test_match_info_point_includes_a_boundary_point(cfg):
    """``covers`` is boundary-inclusive: a point exactly on the outline is this building."""
    roof = box(_LON0, _LAT0, _LON1, _LAT1)
    on_edge = _point_feature(7, _LON0, (_LAT0 + _LAT1) / 2, BAUJAHR=1900)
    match = pipeline.match_info_point(roof, [on_edge], cfg)

    assert match.candidate_count == 1
    assert match.chosen_objectid == 7
    assert match.properties["BAUJAHR"] == 1900
    assert match.ambiguous is False


def test_match_info_point_two_candidates_nearest_to_the_centroid_wins(cfg):
    roof = box(_LON0, _LAT0, _LON1, _LAT1)
    near = _point_feature(2, _LON0 + 0.00045, (_LAT0 + _LAT1) / 2, BAUJAHR=1900)
    far = _point_feature(1, _LON0 + 0.0001, (_LAT0 + _LAT1) / 2, BAUJAHR=1900)
    match = pipeline.match_info_point(roof, [far, near], cfg)

    assert match.candidate_count == 2
    # Nearest wins — NOT the lowest OBJECTID, which was the old undocumented rule.
    assert match.chosen_objectid == 2
    distances = [c["distance_to_roof_centroid_m"] for c in match.candidates]
    assert distances == sorted(distances)
    # Clearly separated (~26 m apart) and agreeing on the published fields: no ambiguity.
    assert match.ambiguous is False


def test_match_info_point_near_tie_is_flagged(cfg):
    """Two points nearly equidistant from the centroid: the ordering proves nothing."""
    roof = box(_LON0, _LAT0, _LON1, _LAT1)
    mid_lat = (_LAT0 + _LAT1) / 2
    left = _point_feature(1, _LON0 + 0.00048, mid_lat, BAUJAHR=1900)
    right = _point_feature(2, _LON0 + 0.00052, mid_lat, BAUJAHR=1900)
    match = pipeline.match_info_point(roof, [left, right], cfg)

    assert match.candidate_count == 2
    assert match.ambiguous is True
    assert "tie epsilon" in match.ambiguity_reason
    # Published from the chosen point all the same — flagged, not withheld.
    assert match.properties["BAUJAHR"] == 1900


def test_match_info_point_material_disagreement_is_flagged(cfg):
    roof = box(_LON0, _LAT0, _LON1, _LAT1)
    mid_lat = (_LAT0 + _LAT1) / 2
    near = _point_feature(1, _LON0 + 0.00045, mid_lat, BAUJAHR=1890)
    far = _point_feature(2, _LON0 + 0.0001, mid_lat, BAUJAHR=1955)
    match = pipeline.match_info_point(roof, [near, far], cfg)

    assert match.ambiguous is True
    assert "disagree on published field BAUJAHR" in match.ambiguity_reason
    assert match.properties["BAUJAHR"] == 1890


def test_an_accepted_ambiguous_enrichment_publishes_with_a_review_flag(cfg, monkeypatch):
    """The above-the-floor ambiguity path end to end, on synthetic matches (no real building
    exercises it): the best candidate is published, the flag fires, the document validates."""
    monkeypatch.setattr(pipeline, "segment_roof", lambda crop, cfg: (None, "not_under_test"))
    monkeypatch.setattr(
        pipeline,
        "match_typology",
        lambda roof, features, cfg_: pipeline.TypologyMatch(
            properties={"OBJECTID": 1, "BAUTYP_TXT": "synthetic typology", "OBJ_STR_TXT": None},
            share_of_roof=0.55,
            share_of_source=0.9,
            candidate_count=2,
            candidates=(
                {
                    "OBJECTID": 1,
                    "overlap_fraction_of_roof": 0.55,
                    "overlap_fraction_of_source_polygon": 0.9,
                },
                {
                    "OBJECTID": 2,
                    "overlap_fraction_of_roof": 0.45,
                    "overlap_fraction_of_source_polygon": 0.8,
                },
            ),
            below_floor=False,
            ambiguous=True,
            ambiguity_reason="synthetic: two typology polygons compete for this roof.",
            min_overlap=0.5,
            ambiguity_margin=0.7,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "match_info_point",
        lambda roof, features, cfg_: pipeline.PointMatch(
            properties={"OBJECTID": 11, "BAUJAHR": 1900},
            candidate_count=2,
            candidates=(
                {"OBJECTID": 11, "distance_to_roof_centroid_m": 3.1},
                {"OBJECTID": 12, "distance_to_roof_centroid_m": 3.4},
            ),
            chosen_objectid=11,
            selection_rule=pipeline.POINT_SELECTION_RULE,
            ambiguous=True,
            ambiguity_reason="synthetic: 2 points nearly equidistant from the roof centroid.",
        ),
    )
    document = pipeline.run(cfg, generated_at=GENERATED_AT_A, write_overlays=False)
    pipeline.validate_output(document)

    for building in document["buildings"]:
        typology = building["attributes"]["building_typology"]
        assert typology["value"] == "synthetic typology"
        assert typology["availability"] == "authoritative"
        assert typology["confidence"]["factors"]["typology_overlap"] == 0.55
        assert typology["source_detail"]["ambiguous"] is True

        year = building["attributes"]["year_built"]
        assert year["value"] == 1900
        assert year["source_detail"]["OBJECTID"] == 11
        assert year["source_detail"]["candidate_count"] == 2

        triggers = [f["trigger"] for f in building["review_flags"]]
        assert "ambiguous_enrichment" in triggers
        assert "multiple_point_candidates" in triggers
        enrichment = next(
            f for f in building["review_flags"] if f["trigger"] == "ambiguous_enrichment"
        )
        assert "compete for this roof" in enrichment["reason"]
        assert "recorded in source_detail" in enrichment["reason"]


# ---------------------------------------------------------------------------------------
# failure isolation: degrade one attribute, never the run (RTI-027)
# ---------------------------------------------------------------------------------------


def test_an_observation_exception_degrades_that_attribute_and_flags_it(cfg, monkeypatch):
    """One attribute's image path exploding must cost that attribute, not the run."""
    monkeypatch.setattr(pipeline, "segment_roof", lambda crop, cfg: (None, "not_under_test"))

    def explode(crop, cfg_):
        raise RuntimeError("synthetic image-path failure")

    monkeypatch.setattr(pipeline.surface, "observe_surface_appearance", explode)
    document = pipeline.run(cfg, generated_at=GENERATED_AT_A, write_overlays=False)
    pipeline.validate_output(document)

    assert len(document["buildings"]) == 10
    for building in document["buildings"]:
        attribute = building["attributes"]["visual_surface_appearance"]
        assert attribute["value"] is None
        assert attribute["availability"] == "unavailable"
        assert attribute["confidence"]["score"] is None
        assert "processing_error" in " ".join(attribute["confidence"]["limitations"])
        assert "RuntimeError" in attribute["confidence"]["rationale"]

        flags = [f for f in building["review_flags"] if f["trigger"] == "processing_error"]
        assert len(flags) == 1
        assert flags[0]["attribute"] == "visual_surface_appearance"
        assert "not a finding about the roof" in flags[0]["reason"]

        # The failure is isolated: the other image attributes are untouched.
        assert building["attributes"]["green_roof"]["value"] is not None or (
            building["attributes"]["green_roof"]["availability"] == "unavailable"
        )
        assert building["attributes"]["roof_area_m2"]["value"] > 0


def test_a_broken_pinned_geometry_is_fatal_and_names_the_building(cfg):
    """Input geometry is validated at load time: the pipeline must not depend on the cache
    verifier having been run (RTI-027)."""
    from dataclasses import replace as dc_replace

    bowtie = {
        "type": "Polygon",
        "coordinates": [
            [
                [16.3790, 48.1850],
                [16.3800, 48.1860],
                [16.3800, 48.1850],
                [16.3790, 48.1860],
                [16.3790, 48.1850],
            ]
        ],
    }
    empty = {"type": "Polygon", "coordinates": []}

    for bad, expectation in ((bowtie, "invalid"), (empty, "empty")):
        records = pipeline.load_roof_records(cfg)
        target = cfg.study_area.buildings[2]
        records[target.objectid] = dc_replace(records[target.objectid], geometry=bad)
        with pytest.raises(pipeline.PipelineError, match=expectation) as raised:
            pipeline.select_pinned(records, cfg)
        assert target.building_id in str(raised.value)


# ---------------------------------------------------------------------------------------
# roof type: the authoritative value, the image evidence, and the conflict flag
# ---------------------------------------------------------------------------------------


def test_no_solar_candidate_positive_or_negative_becomes_a_published_boolean(cfg, buildings):
    """The detector is withheld, so neither verdict may reach the published value.

    A completed non-ground-truth reference evaluation failed its pre-registered false-positive
    bar. This test asserts the demotion holds for *both* directions: a roof where the detector
    said True and one where it said False must publish identically — null, unavailable, unscored
    — with only the raw diagnostics differing. If someone later re-enables the Boolean without
    independent ground truth, this fails.
    """
    verdicts = set()
    for building_id, building in buildings.items():
        solar = building["attributes"]["solar_panels"]
        assert solar["value"] is None, f"{building_id} published a solar verdict"
        assert not isinstance(solar["value"], bool)
        assert solar["availability"] == "unavailable"
        assert solar["confidence"]["score"] is None, f"{building_id} scored a withheld attribute"

        evidence = solar["image_evidence"]
        assert evidence["indicates"] is None
        quality = evidence["quality"]
        assert quality["status"] == "candidate_evidence_withheld_after_failed_evaluation"
        # The detector still ran and its raw output is preserved for future validation.
        assert "withheld_detector_verdict" in quality
        verdicts.add(quality["withheld_detector_verdict"])

    # Both directions are present in this sample, so the assertions above cover both.
    assert verdicts == {True, False}, f"expected both detector verdicts, saw {verdicts}"


def test_the_withheld_solar_detector_still_produced_its_candidate_diagnostics(buildings):
    """Withholding the verdict must not quietly delete the evidence a validation would need."""
    quality = buildings["vie-swv-001"]["attributes"]["solar_panels"]["image_evidence"]["quality"]
    assert quality["clusters"], "raw candidate clusters were dropped"
    assert "coverage_fraction" in quality


def test_a_fired_boundary_alignment_warning_reaches_the_top_level_review_flags(cfg):
    """Routing is tested on SYNTHETIC divergence, because no real building fires the warning.

    Until Phase 4A the warning was measured on the full polygon boundary, which includes
    interior rings. vie-swv-007 therefore fired at 5.705 m — a distance to an unmatched 1.21 m²
    light shaft, not a disagreement about where the roof edge is. Measured on exterior rings the
    same building is 1.476 m apart, well inside the 5.0 m threshold, so the sample now has zero
    fired warnings. Pinning the routing to a real building would mean re-basing this test every
    time the geometry moves; constructed evidence keeps the mechanism verified either way.
    """
    warning = agreement.alignment_warning(
        0.985,  # exterior_iou, comfortably above warn_iou_below
        7.0,  # exterior-ring hausdorff, above warn_hausdorff_above_m
        0.03,
        cfg,
    )
    assert warning["flag"] is True
    assert warning["status"] == "requires_visual_review"

    flags = provenance.review_flags(cfg, boundary_alignment_warning=warning)
    triggers = [flag["trigger"] for flag in flags]
    assert "boundary_alignment_warning" in triggers
    flag = next(f for f in flags if f["trigger"] == "boundary_alignment_warning")
    assert flag["status"] == "requires_visual_review"
    # The reason must not assert the official data is wrong (design section 4.2).
    assert "does not indicate that the authoritative data is incorrect" in flag["reason"]

    # A warning that did not fire routes nothing.
    quiet = agreement.alignment_warning(0.99, 0.4, 0.01, cfg)
    assert quiet["flag"] is False
    assert provenance.review_flags(cfg, boundary_alignment_warning=quiet) == []


def test_the_real_sample_review_flag_map_is_exactly_the_evidence_driven_one(buildings):
    """The honest post-RTI-004 state: no boundary warning fires, and every review flag on the
    real sample comes from the evidence-driven triggers plus the transcribed visual audit —
    never from a conflict, a slope excursion or an ambiguous join, none of which the sample
    exercises. The per-building expectations below are the measured outcomes reported in the
    remediation notes; the thresholds behind them were fixed on principle first."""
    expected_evidence_triggers = {
        "vie-swv-001": set(),
        "vie-swv-002": set(),
        "vie-swv-003": set(),
        "vie-swv-004": set(),
        "vie-swv-005": set(),
        "vie-swv-006": set(),
        "vie-swv-007": {"weak_cv_agreement"},  # topology mismatch (2 rings vs 0)
        "vie-swv-008": set(),
        "vie-swv-009": {"weak_cv_agreement"},  # IoU 0.8971 < 0.90 floor
        # shadow_fraction 0.3175 > 0.25, and two image observations (ridge, green_roof)
        # abstain once the vegetation negative falls under its judgeability floor.
        "vie-swv-010": {"low_judgeability", "withheld_attributes"},
    }
    for building_id, building in buildings.items():
        warning = building["delineation"]["boundary_alignment_warning"] or {}
        assert warning.get("flag") is not True, f"{building_id} unexpectedly fired the warning"
        triggers = {f["trigger"] for f in building["review_flags"]}
        assert triggers - {"visual_audit_questionable"} == expected_evidence_triggers[
            building_id
        ], f"{building_id}: unexpected evidence triggers {triggers}"


def test_embedded_audit_annotations_are_slim_and_point_at_the_full_trail(buildings):
    """RTI-004 slimming: the document embeds aspect/status/code/summary plus a detail_ref
    pointer per building — never the verbatim audit paragraphs, which stay (and stay verbatim)
    in validation/audit_annotations.json. Each embedded summary must be the annotations file's
    own summary for that building/aspect/status: a faithful copy of the distillation, not a
    re-summary and not the note."""
    source = json.loads(
        (Path(__file__).resolve().parents[1] / "validation" / "audit_annotations.json")
        .read_text(encoding="utf-8")
    )
    for building_id, building in buildings.items():
        block = building["manual_review"]
        assert block["detail_ref"] == f"validation/audit_annotations.json#{building_id}"
        source_entries = {entry["aspect"]: entry for entry in source["buildings"][building_id]}
        assert len(block["annotations"]) == len(source_entries)
        for entry in block["annotations"]:
            assert set(entry) == {"aspect", "status", "code", "summary"}
            recorded = source_entries[entry["aspect"]]
            assert entry["status"] == recorded["status"]
            assert entry["code"] == f"{recorded['aspect']}.{recorded['status']}"
            assert entry["summary"] == recorded["summary"]
            assert entry["summary"] != recorded["note"]  # a distillation, not the paragraph


def test_embedded_review_payload_is_capped(buildings):
    """The machine-readable review payload stays concise: audit-derived reasons and embedded
    summaries are pointers plus distillations (< 300 chars), and even the wordiest measured
    evidence reason stays bounded (< 500 chars). The full paragraphs live in validation/."""
    for building in buildings.values():
        for flag in building["review_flags"]:
            assert len(flag["reason"]) < 500, flag["trigger"]
            if flag["trigger"] == "visual_audit_questionable":
                assert len(flag["reason"]) < 300
                assert "Full note: validation/audit_annotations.json" in flag["reason"]
        for entry in building["manual_review"]["annotations"]:
            assert len(entry["summary"]) < 300


def test_vie_swv_007_topology_mismatch_routes_to_weak_cv_agreement(buildings):
    """The real diagnostic that replaced the mis-measured warning.

    Two ~1 m² light shafts in the authoritative outline have no counterpart in the candidate,
    because GrabCut discards holes below its configured minimum area. The diagnostic itself
    still carries no requires_visual_review status of its own, but since RTI-004 the
    structural disagreement is routed into review_flags as weak_cv_agreement, so a reader
    scanning only review_flags no longer misses it.
    """
    building = buildings["vie-swv-007"]
    topology = building["delineation"]["topology_mismatch"]
    assert topology["flag"] is True
    assert topology["authoritative_interior_rings"] == 2
    assert topology["cv_interior_rings"] == 0
    assert topology["authoritative_interior_ring_areas_m2"] == [1.21, 0.69]
    assert topology["cv_interior_ring_areas_m2"] == []
    assert topology["crs"] == "EPSG:31256"
    assert "status" not in topology
    flags = [f for f in building["review_flags"] if f["trigger"] == "weak_cv_agreement"]
    assert len(flags) == 1
    assert "topology" in flags[0]["reason"]

    # The two Hausdorff figures must disagree here, or the fix did nothing.
    agree = building["delineation"]["agreement_with_authoritative_geometry"]
    assert agree["hausdorff_m"] == pytest.approx(1.476, abs=0.01)
    assert agree["hausdorff_full_boundary_m"] == pytest.approx(5.705, abs=0.01)
    assert agree["hausdorff_m"] < agree["hausdorff_full_boundary_m"]


def test_the_epoch_placeholder_is_absent_but_still_traceable(buildings):
    """GEBAEUDETYPOGD's "Keine Angabe" is the source declining to answer, not a value.

    vie-swv-005 is the one building whose typology polygon carries the placeholder. Publishing
    it as an authoritative value asserted 0.902 confidence in a non-answer, and a reader could
    read it as a real epoch.
    """
    epoch = buildings["vie-swv-005"]["attributes"]["construction_epoch"]
    assert epoch["value"] is None
    assert epoch["availability"] == "not_in_source"
    assert epoch["confidence"]["score"] is None

    # Absent, but the bytes behind that judgement stay auditable.
    detail = epoch["source_detail"]
    assert detail["raw_value"] == "Keine Angabe"
    assert detail["OBJ_STR2_TXT"] == "nach 1945"
    # ...and the secondary column is never promoted into the published value.
    assert epoch["value"] != detail["OBJ_STR2_TXT"]


def test_both_run_fingerprints_are_published_and_distinct(document, cfg):
    """config_hash covers the YAML; algorithm_parameters_hash covers the in-code parameters."""
    run = document["run"]
    assert run["config_hash"] == cfg.config_hash
    assert run["algorithm_parameters_hash"] == config.algorithm_parameters_hash()
    assert run["algorithm_parameters_hash"] != run["config_hash"]
    assert re.fullmatch(r"[0-9a-f]{16}", run["algorithm_parameters_hash"])


def test_no_forced_conflict_on_vie_swv_008(buildings):
    """Assert what the detector ACTUALLY produces on this building — not what was hypothesised.

    Reconnaissance recorded a manual reading that the roof looks predominantly flat against
    ``DACHFORM = "Schraegdach"``. The pre-RTI-007 detector reported a ridge at ~65.2 deg there;
    the visual audit identified that line as a deck/facade edge, and the general quality gates
    (medial-axis crest test) now withhold it. So the image evidence abstains: no ridge, no
    image reading, no conflict — and the withheld candidate's gate values stay published in
    the evidence quality. It is deliberately **not** asserted the other way round — a test
    demanding a conflict here would only be satisfiable by retuning the detector until the
    earlier manual guess reappeared (design section 5.1).
    """
    attributes = buildings["vie-swv-008"]["attributes"]
    roof_type = attributes["roof_type"]

    assert roof_type["source_detail"]["raw_value"] == "Schraegdach"
    assert roof_type["value"] == "pitched"
    assert attributes["mean_slope_deg"]["value"] == pytest.approx(42.92, abs=0.01)

    ridge = attributes["ridge_orientation_deg"]
    assert ridge["value"] is None, "008's deck/facade edge must be withheld by the gates"
    gates = ridge["image_evidence"]["quality"]["gates"]
    assert gates["medial_axis_ratio"]["passed"] is False

    assert roof_type["image_evidence"]["indicates"] is None
    assert roof_type.get("conflict") is None
    assert "image_conflict" not in roof_type["confidence"]["factors"]
    triggers = {flag["trigger"] for flag in buildings["vie-swv-008"]["review_flags"]}
    assert "authoritative_image_conflict" not in triggers


def test_no_selected_building_produces_a_conflict_on_the_real_evidence(buildings):
    """The honest position from design 5.1 / risk 2c, asserted rather than assumed."""
    flagged = [
        bid
        for bid, b in buildings.items()
        if any(f["trigger"] == "authoritative_image_conflict" for f in b["review_flags"])
    ]
    assert flagged == []


def test_absence_of_a_ridge_is_never_read_as_evidence_of_a_flat_roof(buildings):
    for building in buildings.values():
        roof_type = building["attributes"]["roof_type"]
        if building["attributes"]["ridge_orientation_deg"]["value"] is None:
            assert roof_type["image_evidence"]["indicates"] is None
            assert roof_type.get("conflict") is None
        else:
            assert roof_type["image_evidence"]["indicates"] == "pitched"


def test_a_synthetic_conflict_produces_the_expected_review_flag(cfg, monkeypatch):
    """The conflict mechanism, tested on constructed evidence that genuinely disagrees.

    Segmentation is stubbed out: it is not under test here and it is ~85% of the run time. The
    document is still validated, which additionally shows that a conflict and a null CV
    candidate coexist in a legal document.
    """
    monkeypatch.setattr(pipeline, "segment_roof", lambda crop, cfg: (None, "not_under_test"))
    monkeypatch.setattr(
        pipeline.roof_form,
        "observe_roof_form",
        lambda crop, cfg, geom=None: ImageObservation(
            name="image_roof_form",
            value="flat",
            method="synthetic_flatness_evidence",
            rationale="synthetic evidence: one dominant plane, no ridge",
        ),
    )
    document = pipeline.run(cfg, generated_at=GENERATED_AT_A, write_overlays=False)
    pipeline.validate_output(document)

    def roof_type_is(value: str) -> list[dict[str, Any]]:
        return [b for b in document["buildings"] if b["attributes"]["roof_type"]["value"] == value]

    pitched = roof_type_is("pitched")
    assert pitched, "expected at least one authoritative Schraegdach in the sample"
    for building in pitched:
        roof_type = building["attributes"]["roof_type"]
        conflict = roof_type["conflict"]
        assert conflict["flag"] is True
        assert conflict["status"] == "requires_visual_review"
        assert "has not been overridden" in conflict["description"]
        # The authoritative value is published unchanged; only the confidence is reduced.
        assert roof_type["value"] == "pitched"
        assert roof_type["source_detail"]["raw_value"] == "Schraegdach"
        factors = roof_type["confidence"]["factors"]
        assert factors["image_conflict"] == float(
            cfg.threshold("confidence", "penalty", "image_conflict")
        )
        assert roof_type["confidence"]["score"] < 0.90
        flags = [
            f
            for f in building["review_flags"]
            if f["trigger"] == "authoritative_image_conflict"
        ]
        assert len(flags) == 1
        assert flags[0]["attribute"] == "roof_type"
        # RTI-004: a conflicted roof_type also carries machine-readable attribute-level
        # review routing.
        assert roof_type["requires_review"] is True
        assert roof_type["review_reasons"]

    for building in roof_type_is("flat"):
        # Authoritative flat + image flat is agreement, not a conflict.
        assert building["attributes"]["roof_type"].get("conflict") is None
        assert "requires_review" not in building["attributes"]["roof_type"]


def test_build_conflict_cannot_alter_the_authoritative_value():
    """Belt and braces on the rule the whole design rests on."""
    conflict = provenance.build_conflict("pitched", "flat")
    assert conflict["flag"] is True
    assert "flat" not in conflict.get("corrected_value", "")
    assert set(conflict) == {"flag", "status", "description"}


def _roof_type_for(dachform, indicates, cfg):
    """build_roof_type on a minimal record + constructed roof-form evidence."""
    from propx_roofs.types import RoofRecord

    record = RoofRecord(
        objectid=1,
        geometry={"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]},
        dachform=dachform,
        slope_mean=None,
        anlagenleistung=None,
        pot_ertrag_y_min=None,
        pot_ertrag_y_max=None,
        suitability_m2={},
        adresse=None,
    )
    form = ImageObservation(
        name="image_roof_form",
        value=indicates,
        method="synthetic",
        rationale="constructed evidence for the conflict matrix",
        quality={"margin": 0.2},
    )
    return pipeline.build_roof_type(record, form, cfg, pipeline.source_names(cfg))


def test_the_conflict_matrix_is_symmetric_and_abstention_is_not_disagreement(cfg):
    """RTI-005: both disagreement directions fire; abstention and agreement never do.

    Synthetic on purpose (see the module docstring): on the current cache the roof-form
    observation indicates pitched only on vie-swv-007 and abstains elsewhere, so neither real
    direction fires — and no threshold is moved to force one.
    """
    # authoritative flat + image pitched -> conflict (the previously reachable direction)
    conflicted = _roof_type_for("Flachdach", "pitched", cfg)
    assert conflicted.conflict and conflicted.conflict["flag"] is True
    # authoritative pitched + image flat -> conflict (previously structurally unreachable)
    conflicted = _roof_type_for("Schraegdach", "flat", cfg)
    assert conflicted.conflict and conflicted.conflict["flag"] is True
    assert conflicted.value == "pitched"  # the authoritative value is never modified
    assert conflicted.requires_review is True
    # agreement in both classes -> no conflict
    assert _roof_type_for("Flachdach", "flat", cfg).conflict is None
    assert _roof_type_for("Schraegdach", "pitched", cfg).conflict is None
    # image abstention -> never a disagreement
    assert _roof_type_for("Flachdach", None, cfg).conflict is None
    assert _roof_type_for("Schraegdach", None, cfg).conflict is None


def test_an_unknown_authoritative_class_never_conflicts_but_the_evidence_stands(cfg):
    """A disagreement with a non-answer is not a disagreement; the image evidence is still
    published on its own so nothing measured is lost."""
    for indicates in ("flat", "pitched", None):
        attribute = _roof_type_for(None, indicates, cfg)
        assert attribute.value == "unknown"
        assert attribute.conflict is None
        assert attribute.image_evidence["indicates"] == indicates
    attribute = _roof_type_for("Tonnendach", "flat", cfg)  # unrecognised raw string
    assert attribute.value == "unknown"
    assert attribute.conflict is None


# ---------------------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------------------


def test_two_runs_are_byte_identical_apart_from_the_run_metadata(cfg, run_a, tmp_path):
    document_a, overlay_dir_a = run_a
    out_b = tmp_path / "run_b"
    document_b = pipeline.run(cfg, generated_at=GENERATED_AT_B, output_dir=out_b)

    assert document_a["run"]["generated_at"] == GENERATED_AT_A
    assert document_b["run"]["generated_at"] == GENERATED_AT_B

    text_a = (overlay_dir_a / "roof_attributes.json").read_text(encoding="utf-8")
    text_b = (out_b / "roof_attributes.json").read_text(encoding="utf-8")
    assert text_a != text_b  # the timestamp really is in the file
    assert text_a.replace(GENERATED_AT_A, "T") == text_b.replace(GENERATED_AT_B, "T")

    # Everything else — including config_hash and every float — is identical.
    stripped_a = json.loads(text_a)
    stripped_b = json.loads(text_b)
    for document in (stripped_a, stripped_b):
        document["run"].pop("generated_at")
    assert stripped_a == stripped_b

    for building_id in PINNED_IDS:
        name = f"overlays/{building_id}.png"
        assert (overlay_dir_a / name).read_bytes() == (out_b / name).read_bytes()


def test_the_run_block_carries_the_config_hash_and_the_required_notes(cfg, document):
    run_block = document["run"]
    assert run_block["config_hash"] == cfg.config_hash
    assert run_block["confidence_note"] == provenance.CONFIDENCE_NOTE
    assert run_block["sample_note"] == provenance.SAMPLE_NOTE
    # The default run attaches the model-assisted baseline QA and its audit trail. A later
    # human review may bind the final artifacts, but does not turn the baseline QA itself into
    # human ground truth or change its reviewer field.
    manual_review = run_block["manual_review"]
    assert manual_review["status"] == "visual_qa_of_baseline_attached"
    assert manual_review["reviewer"] is None
    assert manual_review["n"] == 10
    assert manual_review["audit_basis"]["baseline_commit"] == "c86cbf4"
    annotations = audit.load_annotations(audit.default_annotations_path())
    assert manual_review["audit_basis"] == audit.audit_basis(annotations)
    assert {s["licence"] for s in run_block["sources"]} == {"CC BY 4.0"}
    assert {s["attribution"] for s in run_block["sources"]} == {
        "Datenquelle: Stadt Wien - data.wien.gv.at"
    }


# ---------------------------------------------------------------------------------------
# outputs on disk
# ---------------------------------------------------------------------------------------


def test_overlays_are_written_one_per_building(run_a):
    _, out = run_a
    for building_id in PINNED_IDS:
        path = out / "overlays" / f"{building_id}.png"
        assert path.exists() and path.stat().st_size > 0


def test_every_building_names_its_overlay(document):
    for building in document["buildings"]:
        assert building["overlay"] == f"overlays/{building['building_id']}.png"


def test_overlay_is_null_when_no_overlay_is_written(cfg, tmp_path):
    """``--no-overlays`` must not leave the field naming a PNG that was never written.

    The schema permits ``null`` here precisely so the field can say "there is no overlay"
    rather than pointing a consumer at a path that does not resolve.
    """
    out = tmp_path / "no-overlays"
    document = pipeline.run(
        cfg, output_dir=out, generated_at=GENERATED_AT_A, write_overlays=False
    )

    assert not (out / "overlays").exists()
    for building in document["buildings"]:
        assert building["overlay"] is None


def _exact_colour_pixels(path: Path, colour_bgr: tuple[int, int, int]) -> int:
    """How many pixels of an overlay PNG are exactly ``colour_bgr``."""
    import cv2
    import numpy as np

    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    assert image is not None, f"unreadable overlay {path}"
    return int((image == np.array(colour_bgr, dtype=np.uint8)).all(axis=2).sum())


def test_the_accepted_ridge_segment_is_published_beside_every_published_ridge(buildings):
    """The overlay draws observed evidence, so the evidence must travel in the record: a
    published ridge carries the accepted segment's endpoints, a withheld one carries none."""
    published = 0
    for building_id, building in buildings.items():
        ridge = building["attributes"]["ridge_orientation_deg"]
        quality = ridge["image_evidence"]["quality"]
        if ridge["value"] is None:
            assert "accepted_segment" not in quality, building_id
            continue
        published += 1
        segment = quality["accepted_segment"]
        assert segment["crs"] == "EPSG:4326"
        endpoints = segment["endpoints_lonlat"]
        assert len(endpoints) == 2 and endpoints[0] != endpoints[1]
        # The published azimuth reproduces from the endpoints: the segment IS the evidence.
        reproduced = geometry.azimuth_deg(
            tuple(endpoints[0]), tuple(endpoints[1]), "EPSG:31256"
        )
        assert reproduced == pytest.approx(ridge["value"], abs=0.01)
    assert published >= 1, "expected at least one published ridge in the sample"


def test_the_ridge_segment_is_drawn_on_published_and_never_on_withheld_overlays(
    document, run_a
):
    """Pixel-level check of the rendered PNGs: the observed-ridge colour appears exactly on
    the overlays whose building publishes a ridge (vie-swv-007 here) and on no other —
    withheld cases such as vie-swv-008 and vie-swv-010 must show no ridge line at all."""
    _, out = run_a
    drawn = set()
    for building in document["buildings"]:
        count = _exact_colour_pixels(
            out / building["overlay"], pipeline.OVERLAY_RIDGE_BGR
        )
        if building["attributes"]["ridge_orientation_deg"]["value"] is not None:
            assert count > 0, f"{building['building_id']}: published ridge but no line drawn"
            drawn.add(building["building_id"])
        else:
            assert count == 0, f"{building['building_id']}: withheld ridge but pixels drawn"
    assert drawn == {"vie-swv-007"}
    for withheld_id in ("vie-swv-008", "vie-swv-010"):
        assert withheld_id not in drawn


def test_a_published_ridge_without_endpoints_draws_nothing_rather_than_synthesising():
    """No fallback to a line invented from the azimuth: if the accepted segment is absent,
    ``_accepted_ridge_segment`` yields None and the overlay draws no ridge — and a withheld
    ridge never draws, even if a segment were somehow present in the quality dict."""
    no_segment = {"ridge_orientation_deg": {"value": 65.0, "image_evidence": {"quality": {}}}}
    assert pipeline._accepted_ridge_segment(no_segment) is None

    endpoints = [[16.376, 48.187], [16.377, 48.188]]
    withheld = {
        "ridge_orientation_deg": {
            "value": None,
            "image_evidence": {
                "quality": {"accepted_segment": {"endpoints_lonlat": endpoints}}
            },
        }
    }
    assert pipeline._accepted_ridge_segment(withheld) is None

    accepted = {
        "ridge_orientation_deg": {
            "value": 65.0,
            "image_evidence": {
                "quality": {"accepted_segment": {"endpoints_lonlat": endpoints}}
            },
        }
    }
    assert pipeline._accepted_ridge_segment(accepted) == endpoints


def test_a_vegetation_negative_below_the_judgeability_floor_abstains_in_the_document(
    buildings,
):
    """The general negative judgeability floor, seen end to end on the real sample:
    vie-swv-010 (68.2% judgeable, under the 75% floor) publishes null with abstention
    semantics and its diagnostics intact; the well-illuminated negatives stand."""
    green = buildings["vie-swv-010"]["attributes"]["green_roof"]
    assert green["value"] is None
    assert green["availability"] == "unavailable"
    assert green["confidence"]["score"] is None
    assert "abstention" in " ".join(green["confidence"]["limitations"])
    quality = green["image_evidence"]["quality"]
    floor = quality["thresholds"]["negative_min_judgeable_fraction"]
    assert quality["vegetation_judgeable_fraction"] < floor
    # Raw vegetation diagnostics are preserved through the abstention.
    assert quality["vegetated_fraction"] is not None
    assert quality["mean_exg"] is not None

    for building_id in ("vie-swv-001", "vie-swv-007"):
        other = buildings[building_id]["attributes"]["green_roof"]
        assert other["value"] == "not_detected", building_id
        other_quality = other["image_evidence"]["quality"]
        assert other_quality["vegetation_judgeable_fraction"] >= floor


def test_the_overlay_green_roof_caption_keeps_abstention_distinct_from_a_negative():
    """``None`` must not be drawn as "not detected".

    The caption used to test truthiness, so an abstention and a negative finding produced
    identical words on the image — the one conflation the whole abstention design exists to
    prevent. Thresholds and JSON values are not involved here; only the caption text is.
    """
    detected = pipeline._green_roof_caption("detected")
    absent = pipeline._green_roof_caption("not_detected")
    unknown = pipeline._green_roof_caption(None)

    assert detected.startswith("detected")
    assert absent.startswith("not detected")
    assert unknown.startswith("unavailable")

    # The three must be mutually distinguishable, and the abstention must not contain the
    # word a reader would parse as a negative finding.
    assert len({detected, absent, unknown}) == 3
    assert "not detected" not in unknown
    # Both real verdicts stay hedged: dormant-season RGB is weak evidence either way.
    assert "low confidence" in detected and "low confidence" in absent
