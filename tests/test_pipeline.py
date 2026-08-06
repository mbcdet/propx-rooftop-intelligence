"""End-to-end behaviour of the wiring, asserted against the real committed cache.

These tests run the actual pipeline over the ten pinned buildings. That is slow enough to be
worth justifying: the properties being checked here — that the primary polygon is never a CV
product, that a missing source becomes ``not_in_source`` and not zero, that every published
metre came from EPSG:31256 — are properties of the *composition*, and a mocked run would assert
them about the mocks.

Two rules govern what is asserted about content:

* **Nothing is asserted into existence.** The vie-swv-008 test asserts what the implemented
  detector actually produces (a ridge that *supports* the authoritative ``Schraegdach``), and
  explicitly asserts that no conflict is flagged. Writing a test that demanded a conflict there
  would have forced the detector to be retuned until it produced one.
* **The mechanisms that have no exemplar in the sample are tested synthetically** — the CV
  failure path and the authoritative-versus-image conflict — so their correctness never depends
  on a real building behaving a particular way.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from shapely.geometry import box

from propx_roofs import config, geometry, pipeline, provenance, units
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
    """Design risk 2b: 3 of 10 have typology, 1 of 10 has a BAUJAHR. Assert the real coverage."""
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
    assert with_typology == ["vie-swv-005", "vie-swv-007", "vie-swv-010"]
    assert with_year == ["vie-swv-009"]
    assert buildings["vie-swv-009"]["attributes"]["year_built"]["value"] == 1891


def test_typology_confidence_reflects_the_published_overlap(buildings):
    """A weak best-overlap match must not keep full authoritative confidence."""
    expected = {
        "vie-swv-005": (0.6649, 0.6649, 0.600),
        "vie-swv-007": (0.4910, 0.5000, 0.451),
        "vie-swv-010": (0.4479, 0.5000, 0.451),
    }
    for building_id, (overlap, factor, score) in expected.items():
        attribute = buildings[building_id]["attributes"]["building_typology"]
        assert attribute["source_detail"]["overlap_fraction_of_roof"] == overlap
        assert attribute["confidence"]["factors"]["typology_overlap"] == factor
        assert attribute["confidence"]["score"] == score


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


def test_no_real_building_fires_a_boundary_warning_or_carries_a_review_flag(buildings):
    """The honest post-Phase-4A state of the sample: zero fired warnings, zero review flags."""
    for building_id, building in buildings.items():
        warning = building["delineation"]["boundary_alignment_warning"] or {}
        assert warning.get("flag") is not True, f"{building_id} unexpectedly fired the warning"
        assert building["review_flags"] == [], f"{building_id} carries an unexpected review flag"


def test_vie_swv_007_reports_a_topology_mismatch_without_a_review_flag(buildings):
    """The real diagnostic that replaced the mis-measured warning.

    Two ~1 m² light shafts in the authoritative outline have no counterpart in the candidate,
    because GrabCut discards holes below its configured minimum area. That is expected segmenter
    behaviour, so it is reported as a diagnostic and deliberately routes nowhere.
    """
    building = buildings["vie-swv-007"]
    topology = building["delineation"]["topology_mismatch"]
    assert topology["flag"] is True
    assert topology["authoritative_interior_rings"] == 2
    assert topology["cv_interior_rings"] == 0
    assert topology["authoritative_interior_ring_areas_m2"] == [1.21, 0.69]
    assert topology["cv_interior_ring_areas_m2"] == []
    assert topology["crs"] == "EPSG:31256"
    # A diagnostic, not a review trigger: no status key, and nothing in review_flags.
    assert "status" not in topology
    assert building["review_flags"] == []

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
    ``DACHFORM = "Schraegdach"``. The implemented ridge detector does not support that reading:
    it finds a ridge at ~65.2 deg, which is evidence *for* the authoritative class. So this test
    asserts the absence of a conflict. It is deliberately **not** written the other way round —
    a test demanding a conflict here would only be satisfiable by retuning the detector until
    the earlier guess reappeared (design section 5.1).
    """
    attributes = buildings["vie-swv-008"]["attributes"]
    roof_type = attributes["roof_type"]

    assert roof_type["source_detail"]["raw_value"] == "Schraegdach"
    assert roof_type["value"] == "pitched"
    assert attributes["mean_slope_deg"]["value"] == pytest.approx(42.92, abs=0.01)

    ridge = attributes["ridge_orientation_deg"]["value"]
    assert ridge is not None
    assert ridge == pytest.approx(65.2, abs=1.0)

    assert roof_type["image_evidence"]["indicates"] == "pitched"
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
        pipeline,
        "image_roof_type",
        lambda ridge: ("flat", "synthetic evidence: one dominant plane, no ridge"),
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

    for building in roof_type_is("flat"):
        assert building["attributes"]["roof_type"].get("conflict") is None


def test_build_conflict_cannot_alter_the_authoritative_value():
    """Belt and braces on the rule the whole design rests on."""
    conflict = provenance.build_conflict("pitched", "flat")
    assert conflict["flag"] is True
    assert "flat" not in conflict.get("corrected_value", "")
    assert set(conflict) == {"flag", "status", "description"}


def test_image_roof_type_abstains_without_a_ridge():
    absent = ImageObservation(
        name="ridge_orientation_deg", value=None, method="m", rationale="no ridge detected"
    )
    present = ImageObservation(
        name="ridge_orientation_deg", value=65.24, method="m", rationale="a ridge was detected"
    )
    assert pipeline.image_roof_type(absent)[0] is None
    assert pipeline.image_roof_type(present)[0] == "pitched"


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
    assert run_block["manual_review"] == {"status": "not_yet_reviewed", "reviewer": None, "n": None}
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


def test_the_overlay_green_roof_caption_keeps_abstention_distinct_from_a_negative():
    """``None`` must not be drawn as "not detected".

    The caption used to test truthiness, so an abstention and a negative finding produced
    identical words on the image — the one conflation the whole abstention design exists to
    prevent. Thresholds and JSON values are not involved here; only the caption text is.
    """
    detected = pipeline._green_roof_caption(True)
    absent = pipeline._green_roof_caption(False)
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
