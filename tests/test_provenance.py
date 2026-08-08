"""Provenance: the licence text, the notes, and a conflict that changes nothing.

The two notes are checked against their source documents rather than against a copy of
themselves — the purposive-sample caveat is required verbatim, and a test that compares a
constant to itself would let a paraphrase through.

The centrepiece is a conflict case built on **synthetic image evidence** that genuinely
disagrees with an authoritative ``DACHFORM``. It borrows vie-swv-008's identifiers and
authoritative values (OBJECTID 358486, ``Schraegdach`` at ``SLOPE_MEAN = 42.92``) for realism,
but the disagreeing image observation is constructed here, not taken from the detector.

That distinction matters. The implemented ridge detector reports a ridge at ~65.2 deg on the
real vie-swv-008, which *supports* the authoritative class, so the real building does not
trigger the flag. Testing the mechanism against constructed evidence keeps it verified without
requiring any real building to behave a particular way — and without tempting anyone to retune
the detector until a preferred building lights up.

The pipeline's job when evidence does conflict: publish the authoritative value unchanged,
publish the image observation beside it, raise a flag, and resolve nothing.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from propx_roofs import confidence, config, provenance, schema
from propx_roofs.types import Attribute, Availability, CvDelineation, JoinEvidence

ROOF_LAYER = "PV-Potenzial 2025 (ogdwien:ANLAGENLEISTUNG2025OGD)"
IMAGERY = "Orthofoto 2024 Wien"

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


@pytest.fixture(scope="module")
def run(cfg: config.Config) -> dict[str, Any]:
    return provenance.run_block(cfg, datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc))


def _join(**overrides: Any) -> JoinEvidence:
    base = {
        "matched_by": "best_geometric_overlap",
        "cardinality": "1:1",
        "bw_geb_id": 5611476,
        "iou": 0.967,
        "containment_roof_in_unit": 0.983,
        "containment_unit_in_roof": 0.981,
        "second_best_iou": 0.041,
        "second_best_bw_geb_id": None,
        "ambiguous": False,
        "ambiguity_reasons": (),
        "fmzk_parts_in_unit": 11,
        "fmzk_parts_null_id_in_area": 105,
        "fmzk_fetch_buffer_m": 150.0,
        "unit_touches_fetch_boundary": False,
    }
    return JoinEvidence(**{**base, **overrides})


# --- 9. the run block ------------------------------------------------------------------------


def test_run_block_carries_the_identifiers_the_output_is_reproducible_from(
    cfg: config.Config, run: dict[str, Any]
) -> None:
    assert run["generated_at"] == "2026-08-03T12:00:00+00:00"
    assert run["config_hash"] == cfg.config_hash
    assert run["study_area"]["name"] == "sonnwendviertel"
    assert run["study_area"]["crs_metric"] == "EPSG:31256"
    assert run["study_area"]["imagery_zoom"] == 20
    assert run["study_area"]["bbox_wgs84"] == [16.3760, 48.1830, 16.3820, 48.1880]


def test_run_block_records_schema_version_from_the_packaged_schema(run: dict[str, Any]) -> None:
    """RTI-020: the version is the schema's own const, never a second copy in Python."""
    declared = schema.load_schema()["$defs"]["run"]["properties"]["schema_version"]["const"]
    assert run["schema_version"] == declared
    assert run["schema_version"] == provenance.schema_version()
    # And the schema refuses any other value, so a stale stamp cannot validate.
    assert "schema_version" in schema.load_schema()["$defs"]["run"]["required"]


def test_run_block_records_the_git_commit_and_dirty_flag(run: dict[str, Any]) -> None:
    """This test runs inside the checkout, so a real SHA must be captured."""
    git = run["git"]
    assert isinstance(git["commit"], str) and re.fullmatch(r"[0-9a-f]{40}", git["commit"])
    assert isinstance(git["dirty"], bool)


def test_git_provenance_records_an_honest_null_when_git_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed wheel outside a repo must get nulls with a reason, never a crash."""
    import subprocess as subprocess_module

    def no_git(*_args: Any, **_kwargs: Any):
        raise FileNotFoundError("git")

    monkeypatch.setattr(provenance.subprocess, "run", no_git)
    block = provenance.git_provenance()
    assert block["commit"] is None
    assert block["dirty"] is None
    assert "git executable not available" in block["note"]

    def not_a_repo(*args: Any, **kwargs: Any):
        raise subprocess_module.CalledProcessError(128, args[0])

    monkeypatch.setattr(provenance.subprocess, "run", not_a_repo)
    block = provenance.git_provenance()
    assert block["commit"] is None
    assert "not a git checkout" in block["note"]


def test_run_block_records_runtime_and_dependency_versions(run: dict[str, Any]) -> None:
    """RTI-001/020: matching hashes are not enough; the runtime and libraries are recorded."""
    import platform as platform_module

    assert run["runtime"]["python"] == platform_module.python_version()
    assert run["runtime"]["platform"] == platform_module.platform()

    dependencies = run["dependencies"]
    assert set(dependencies) == set(provenance.TRACKED_DEPENDENCIES)
    # Everything is installed in the test environment, so every version must be a real string.
    for name, version in dependencies.items():
        assert isinstance(version, str) and version, f"{name} has no recorded version"
    # requests is deliberately untracked: the offline run never imports it.
    assert "requests" not in dependencies


def test_a_run_block_with_the_new_provenance_fields_validates(
    cfg: config.Config, run: dict[str, Any]
) -> None:
    """The schema requires the RTI-020 fields, so run_block and the schema move together."""
    required = schema.load_schema()["$defs"]["run"]["required"]
    for fields in ("schema_version", "git", "runtime", "dependencies"):
        assert fields in required
        assert fields in run


def test_every_source_carries_the_licence_and_the_exact_attribution(run: dict[str, Any]) -> None:
    """CC BY obligations travel with the data, so they are per-source, not a footnote."""
    assert len(run["sources"]) == 5
    for source in run["sources"]:
        assert source["licence"] == "CC BY 4.0"
        assert source["attribution"] == "Datenquelle: Stadt Wien - data.wien.gv.at"
        assert source["accessed"] == "2026-08-03"
        assert source["url"].startswith("https://")


def test_licence_and_attribution_are_read_per_source_from_the_config(
    cfg: config.Config,
) -> None:
    """RTI-019: the recorded terms are each source's own config fields, not a global constant.

    A future differently-licensed source must be a config edit; the constants in config.py are
    only the documented defaults for entries that omit the fields.
    """
    sources = {k: dict(v) for k, v in cfg.study_area.sources.items()}
    # Every committed entry now carries the fields explicitly.
    for key, raw in sources.items():
        assert raw["licence"] == "CC BY 4.0", key
        assert raw["attribution"] == "Datenquelle: Stadt Wien - data.wien.gv.at", key

    # A hypothetical differently-licensed source is honoured verbatim...
    sources["typology"]["licence"] = "CC BY-SA 4.0"
    sources["typology"]["attribution"] = "Datenquelle: Beispielanbieter"
    sources["typology"]["terms_url"] = "https://example.org/terms"
    # ...and an entry that omits the fields falls back to the documented defaults.
    del sources["building_info"]["licence"]
    del sources["building_info"]["attribution"]

    edited = replace(cfg, study_area=replace(cfg.study_area, sources=sources))
    by_layer = {s.layer: s for s in provenance.source_descriptors(edited)}

    typology = by_layer["ogdwien:GEBAEUDETYPOGD"]
    assert typology.licence == "CC BY-SA 4.0"
    assert typology.attribution == "Datenquelle: Beispielanbieter"
    assert typology.terms_url == "https://example.org/terms"
    assert typology.as_dict()["terms_url"] == "https://example.org/terms"

    info = by_layer["ogdwien:GEBAEUDEINFOOGD"]
    assert info.licence == config.LICENCE
    assert info.attribution == config.ATTRIBUTION
    assert info.terms_url is None
    assert "terms_url" not in info.as_dict()

    # Distinct terms produce distinct attribution lines for the README/overlays.
    lines = provenance.attribution_lines(provenance.source_descriptors(edited))
    assert any("Beispielanbieter" in line and "CC BY-SA 4.0" in line for line in lines)


def test_run_block_pins_the_cache_manifest_and_records_a_real_verification(
    cfg: config.Config, run: dict[str, Any]
) -> None:
    """RTI-011: run.cache carries the manifest's sha256 and a measured (not asserted) verdict."""
    import hashlib

    manifest_path = cfg.study_area.cache_dir / "manifest.json"
    expected = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert run["cache"]["manifest_sha256"] == expected
    assert run["cache"]["verified"] is True, "the committed, hash-pinned cache must verify"


def test_cache_provenance_reports_an_honest_null_for_a_missing_cache(
    cfg: config.Config, tmp_path: Any
) -> None:
    edited = replace(cfg, study_area=replace(cfg.study_area, cache_root=tmp_path / "nowhere"))
    block = provenance.cache_provenance(edited)
    assert block["manifest_sha256"] is None
    assert block["verified"] is False
    assert "no cache manifest" in block["note"]


def test_the_roof_record_source_states_the_model_basis(run: dict[str, Any]) -> None:
    """A reader has to be able to see that the roof record is not the 2024 orthophoto."""
    roof = next(s for s in run["sources"] if "ANLAGENLEISTUNG2025OGD" in str(s.get("layer")))
    assert roof["model_basis"] == (
        "2023 laser-scan surface model (DOM) at 7.5 cm; 2025 aerial imagery; "
        "1 m DGM for far shading"
    )
    assert roof["model_basis"] == provenance.MODEL_BASIS

    imagery = next(s for s in run["sources"] if s["name"].startswith(IMAGERY))
    assert imagery["resolution_m"] == 0.15
    assert imagery["year"] == 2024
    assert imagery["flight_dates"][0] == "2024-03-19"


def test_a_source_without_a_fetch_date_is_refused(cfg: config.Config) -> None:
    """Undated data cannot be described as current, so the omission has to fail."""
    sources = {k: dict(v) for k, v in cfg.study_area.sources.items()}
    sources["typology"].pop("fetched")
    broken = replace(cfg, study_area=replace(cfg.study_area, sources=sources))
    with pytest.raises(ValueError, match="has no 'fetched' date"):
        provenance.run_block(broken, "2026-08-03")


def test_the_confidence_note_never_claims_calibration(run: dict[str, Any]) -> None:
    assert run["confidence_note"] == (
        "Heuristic reliability indicators, not calibrated probabilities. Not validated against "
        "labelled ground truth."
    )
    assert "not calibrated probabilities" in run["confidence_note"]


def test_the_sample_note_is_verbatim_from_the_selection_document(run: dict[str, Any]) -> None:
    """The caveat is required verbatim, so it is checked against the document, not a copy."""
    doc = (config.REPO_ROOT / "docs" / "study_area_selection.md").read_text(encoding="utf-8")
    flattened = re.sub(r"\s*\n>?\s*", " ", doc.replace("> ", ""))

    assert provenance.SAMPLE_NOTE in flattened
    assert run["sample_note"] == provenance.SAMPLE_NOTE
    assert "not statistically representative" in run["sample_note"]


def test_manual_review_starts_unreviewed_and_is_a_fresh_object(run: dict[str, Any]) -> None:
    """Section 7: review is QA. This block reports whether anyone looked, and nothing else."""
    assert run["manual_review"] == {"status": "not_yet_reviewed", "reviewer": None, "n": None}

    first = provenance.manual_review_block()
    first["status"] = "tampered"
    assert provenance.manual_review_block()["status"] == "not_yet_reviewed"


def test_provenance_exposes_no_way_to_write_a_review_back() -> None:
    """A mutating helper here is how "QA only" quietly stops being true."""
    forbidden = ("apply_review", "merge_review", "set_review", "write_review", "update")
    assert [name for name in dir(provenance) if name in forbidden] == []


# --- 6. conflicts ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("authoritative", "image"),
    [("pitched", "pitched"), ("flat", "Flat"), ("flat", " flat "), (True, True)],
)
def test_agreement_produces_no_conflict(authoritative: Any, image: Any) -> None:
    assert provenance.build_conflict(authoritative, image) is None


def test_an_abstaining_image_stage_is_not_a_disagreement() -> None:
    """Otherwise every shadowed roof would raise a flag and the flag would mean nothing."""
    assert provenance.build_conflict("pitched", None) is None


def test_disagreement_produces_a_flag_and_leaves_the_authoritative_value_alone() -> None:
    authoritative = "pitched"
    conflict = provenance.build_conflict(
        authoritative,
        "flat",
        "Authoritative DACHFORM indicates pitched; image evidence indicates a predominantly "
        "flat roof.",
    )

    assert conflict is not None
    assert conflict["flag"] is True
    assert conflict["status"] == "requires_visual_review"
    assert "has not been overridden" in conflict["description"]
    # Nothing in the conflict block is a corrected value, and the input is untouched.
    assert authoritative == "pitched"
    assert "flat" not in {v for k, v in conflict.items() if k != "description"}


def test_a_default_description_names_both_values() -> None:
    conflict = provenance.build_conflict("pitched", "flat")
    assert conflict is not None
    assert "'pitched'" in conflict["description"] and "'flat'" in conflict["description"]
    assert conflict["description"].endswith(provenance.CONFLICT_DESCRIPTION_TAIL)


# --- review flags -----------------------------------------------------------------------------


def test_no_triggers_means_no_flags(cfg: config.Config) -> None:
    assert provenance.review_flags(cfg, slope_mean_deg=42.92, join=_join()) == []


def test_the_slope_trigger_fires_only_above_the_configured_threshold(cfg: config.Config) -> None:
    """42.92 deg is steep but well under the 60 deg review threshold, so it must not flag."""
    limit = cfg.threshold("roof_type", "slope_review_above_deg")
    assert limit == 60.0

    assert provenance.review_flags(cfg, slope_mean_deg=limit) == []
    flags = provenance.review_flags(cfg, slope_mean_deg=77.0)
    assert [f["trigger"] for f in flags] == ["slope_mean_above_review_threshold"]
    assert "77.0 deg exceeds the 60.0 deg review threshold" in flags[0]["reason"]
    assert "published unchanged" in flags[0]["reason"]


def test_an_ambiguous_join_is_flagged_with_its_reasons(cfg: config.Config) -> None:
    join = _join(ambiguous=True, ambiguity_reasons=("second_best_iou/best_iou 0.81 > 0.70",))
    flags = provenance.review_flags(cfg, join=join)
    assert [f["trigger"] for f in flags] == ["ambiguous_join"]
    assert "0.81 > 0.70" in flags[0]["reason"]
    assert "documented heuristics" in flags[0]["reason"]


def test_the_enrichment_and_processing_triggers_fire_with_reasons(cfg: config.Config) -> None:
    """The RTI-012/013/027 triggers: reasons pre-built by the pipeline, shaped here."""
    flags = provenance.review_flags(
        cfg,
        ambiguous_enrichment=("synthetic: two GEBAEUDETYPOGD polygons compete for this roof.",),
        multiple_point_candidates=(
            "synthetic: 2 GEBAEUDEINFOOGD points fall on this roof outline and are nearly "
            "equidistant."
        ),
        processing_errors=(("green_roof", "ValueError: synthetic image failure"),),
    )

    assert [f["trigger"] for f in flags] == [
        "ambiguous_enrichment",
        "multiple_point_candidates",
        "processing_error",
    ]
    assert all(f["status"] == "requires_visual_review" for f in flags)
    assert "compete for this roof" in flags[0]["reason"]
    assert "documented heuristic" in flags[0]["reason"]
    assert "recorded in source_detail" in flags[1]["reason"]
    assert flags[2]["attribute"] == "green_roof"
    assert "ValueError: synthetic image failure" in flags[2]["reason"]
    assert "not a finding about the roof" in flags[2]["reason"]

    # The packaged schema's trigger vocabulary accepts all three, so a record carrying them
    # is publishable.
    triggers = schema.load_schema()["$defs"]["review_flag"]["properties"]["trigger"]["enum"]
    for flag in flags:
        assert flag["trigger"] in triggers


def test_all_three_triggers_can_fire_together(cfg: config.Config) -> None:
    flags = provenance.review_flags(
        cfg,
        roof_type_conflict=provenance.build_conflict("pitched", "flat"),
        slope_mean_deg=77.0,
        join=_join(ambiguous=True, ambiguity_reasons=("best_iou 0.31 < 0.50",)),
    )
    assert [f["trigger"] for f in flags] == [
        "authoritative_image_conflict",
        "slope_mean_above_review_threshold",
        "ambiguous_join",
    ]
    assert all(f["status"] == "requires_visual_review" for f in flags)
    assert all(f["reason"] for f in flags)


# --- 10. a synthetic conflict, end to end -----------------------------------------------------


def _vie_swv_008(cfg: config.Config) -> tuple[dict[str, Any], dict[str, Any]]:
    """A conflict case as it would be published: three fields, none of them resolved.

    Authoritative values and identifiers are vie-swv-008's, for realism. The disagreeing image
    observation below is **synthetic** — the real detector finds a ridge on that building and
    agrees with the authoritative class, so this exercises the mechanism without asserting
    anything false about the building.
    """
    conflict = provenance.build_conflict(
        "pitched",
        "flat",
        "Authoritative DACHFORM indicates pitched; image evidence indicates a predominantly "
        "flat roof with HVAC plant, skylight strips and a raised penthouse.",
    )
    assert conflict is not None

    roof_type = Attribute(
        value="pitched",
        availability=Availability.AUTHORITATIVE,
        confidence=confidence.build(
            Availability.AUTHORITATIVE,
            "authoritative_dachform",
            "authoritative roof form for this exact outline; reduced because image evidence "
            "disagrees",
            (ROOF_LAYER,),
            {
                "source_recency": confidence.penalty("source_recency", cfg=cfg),
                "image_conflict": confidence.penalty("image_conflict", cfg=cfg),
            },
            (
                "binary flat/pitched; cannot express gable, hip or mansard",
                "the conflict factor is a documented heuristic, not a probability that the "
                "source is wrong",
            ),
            confidence.positive_cap("roof_type_authoritative", cfg=cfg),
            cfg=cfg,
        ),
        source_detail={
            "layer": "ogdwien:ANLAGENLEISTUNG2025OGD",
            "field": "DACHFORM",
            "raw_value": "Schraegdach",
        },
        image_evidence={
            "indicates": "flat",
            "method": "plane_brightness_and_ridge_analysis",
            "rationale": "single dominant plane, no ridge detected",
        },
        conflict=conflict,
    )

    slope = Attribute(
        value=42.92,
        availability=Availability.AUTHORITATIVE,
        confidence=confidence.build(
            Availability.AUTHORITATIVE,
            "authoritative_single_record",
            "taken directly from the single matched roof record",
            (ROOF_LAYER,),
            {"source_recency": confidence.penalty("source_recency", cfg=cfg)},
            ("mean over the whole roof, not per plane",),
            cfg=cfg,
        ),
        unit="deg",
        source_detail={
            "layer": "ogdwien:ANLAGENLEISTUNG2025OGD",
            "field": "SLOPE_MEAN",
            "records_matched": 1,
            "aggregated": False,
        },
    )

    area = Attribute(
        value=677.4,
        availability=Availability.DERIVED,
        confidence=confidence.build(
            Availability.DERIVED,
            "authoritative_outline_area_epsg31256",
            "planimetric area of the authoritative outline in EPSG:31256",
            (ROOF_LAYER,),
            {},
            ("planimetric, not roof-surface area",),
            cfg=cfg,
        ),
        unit="m2",
        measure="planimetric",
    )

    delineation = CvDelineation(
        segmenter="grabcut+geometric_refinement",
        polygon=POLYGON,
        iou=0.91,
        symmetric_difference_ratio=0.10,
        hausdorff_m=1.1,
        boundary_gradient_ratio=0.88,
        alignment_warning={"flag": False, "metrics": None, "note": None},
    ).as_dict()
    delineation["primary_polygon"] = "authoritative_roof_polygon"

    building = {
        "building_id": "vie-swv-008",
        "source_ids": {
            "anlagenleistung2025_objectid": 358486,
            "bw_geb_id": 5611476,
            "fmzk_ids": [],
        },
        "authoritative_roof_polygon": {
            **POLYGON,
            "polygon_source": "ogdwien:ANLAGENLEISTUNG2025OGD",
        },
        "cv_candidate_polygon": POLYGON,
        "delineation": delineation,
        "fmzk_crosscheck": _join().as_dict(),
        "attributes": {
            "roof_area_m2": area.as_dict(),
            "roof_type": roof_type.as_dict(),
            "mean_slope_deg": slope.as_dict(),
        },
        "review_flags": provenance.review_flags(
            cfg, roof_type_conflict=conflict, slope_mean_deg=42.92, join=_join()
        ),
    }
    return building, conflict


def test_a_synthetic_conflict_publishes_authoritative_value_image_evidence_and_a_flag(
    cfg: config.Config, run: dict[str, Any]
) -> None:
    building, _ = _vie_swv_008(cfg)
    roof_type = building["attributes"]["roof_type"]

    # The authoritative class survives intact, raw source string included.
    assert roof_type["value"] == "pitched"
    assert roof_type["source_detail"]["raw_value"] == "Schraegdach"
    assert roof_type["availability"] == "authoritative"

    # The image observation is beside it, not instead of it.
    assert roof_type["image_evidence"]["indicates"] == "flat"

    # And the disagreement is visible as a flag, on the attribute and on the building.
    assert roof_type["conflict"]["status"] == "requires_visual_review"
    assert [f["trigger"] for f in building["review_flags"]] == ["authoritative_image_conflict"]
    assert building["review_flags"][0]["attribute"] == "roof_type"

    # 0.95 x 0.95 x 0.70 = 0.63175 -> 0.632, under the 0.90 cap. The reduction is labelled a
    # heuristic in the limitations rather than presented as a probability (section 6.2).
    assert roof_type["confidence"]["score"] == 0.632
    assert roof_type["confidence"]["factors"] == {"source_recency": 0.95, "image_conflict": 0.70}
    assert any("documented heuristic" in x for x in roof_type["confidence"]["limitations"])

    # The whole document is valid, so this shape is publishable as it stands.
    schema.validate({"run": run, "buildings": [building]})


def test_the_conflict_does_not_touch_the_slope_or_the_polygon(cfg: config.Config) -> None:
    """Only ``roof_type`` is affected; a conflict is not a licence to discount the record."""
    building, _ = _vie_swv_008(cfg)

    assert building["attributes"]["mean_slope_deg"]["value"] == 42.92
    assert building["attributes"]["mean_slope_deg"]["confidence"]["factors"] == {
        "source_recency": 0.95
    }
    assert building["delineation"]["primary_polygon"] == "authoritative_roof_polygon"
    assert building["authoritative_roof_polygon"]["polygon_source"] == (
        "ogdwien:ANLAGENLEISTUNG2025OGD"
    )


# --- evidence-driven review triggers (RTI-004) ------------------------------------------------


def test_low_judgeability_fires_on_either_measurement(cfg: config.Config) -> None:
    """Both the judgeable fraction and the shadow fraction can cross their line; either is
    enough, and the reason names the measured number and its threshold."""
    flags = provenance.review_flags(
        cfg, image_quality={"judgeable_fraction": 0.60, "shadow_fraction": 0.10}
    )
    assert [f["trigger"] for f in flags] == ["low_judgeability"]
    assert "60.0%" in flags[0]["reason"]

    flags = provenance.review_flags(
        cfg, image_quality={"judgeable_fraction": 0.90, "shadow_fraction": 0.30}
    )
    assert [f["trigger"] for f in flags] == ["low_judgeability"]
    assert "30.0%" in flags[0]["reason"]

    # Both crossing still fires exactly once, with both measurements in the reason.
    flags = provenance.review_flags(
        cfg, image_quality={"judgeable_fraction": 0.60, "shadow_fraction": 0.30}
    )
    assert [f["trigger"] for f in flags] == ["low_judgeability"]


def test_a_readable_roof_fires_no_low_judgeability(cfg: config.Config) -> None:
    assert (
        provenance.review_flags(
            cfg, image_quality={"judgeable_fraction": 1.0, "shadow_fraction": 0.0}
        )
        == []
    )
    # Unmeasurable quantities never fire: an unknown is not evidence of a problem.
    assert (
        provenance.review_flags(
            cfg, image_quality={"judgeable_fraction": None, "shadow_fraction": None}
        )
        == []
    )


def test_weak_cv_agreement_fires_below_the_iou_floor(cfg: config.Config) -> None:
    flags = provenance.review_flags(cfg, cv_agreement={"iou": 0.85, "topology_mismatch": None})
    assert [f["trigger"] for f in flags] == ["weak_cv_agreement"]
    assert "0.8500" in flags[0]["reason"]
    assert "not that either is wrong" in flags[0]["reason"]


def test_weak_cv_agreement_fires_on_a_topology_mismatch(cfg: config.Config) -> None:
    flags = provenance.review_flags(
        cfg, cv_agreement={"iou": 0.97, "topology_mismatch": {"flag": True}}
    )
    assert [f["trigger"] for f in flags] == ["weak_cv_agreement"]
    assert "topology" in flags[0]["reason"]


def test_strong_cv_agreement_fires_nothing(cfg: config.Config) -> None:
    assert (
        provenance.review_flags(
            cfg, cv_agreement={"iou": 0.97, "topology_mismatch": {"flag": False}}
        )
        == []
    )
    # No candidate at all: there is no agreement to be weak, and the null CV outcome is
    # already reported in the delineation.
    assert provenance.review_flags(cfg, cv_agreement=None) == []


def test_withheld_attributes_fires_at_the_configured_count(cfg: config.Config) -> None:
    minimum = int(cfg.threshold("review", "withheld_attributes_min"))
    below = ("ridge_orientation_deg",) * (minimum - 1)
    assert provenance.review_flags(cfg, withheld_attributes=below) == []

    at = ("visual_surface_appearance", "ridge_orientation_deg")[:minimum] + (
        ("green_roof",) * max(0, minimum - 2)
    )
    flags = provenance.review_flags(cfg, withheld_attributes=at)
    assert [f["trigger"] for f in flags] == ["withheld_attributes"]
    assert "never a negative" in flags[0]["reason"]


def test_visual_audit_routing_flags_doubt_and_only_doubt(cfg: config.Config) -> None:
    """supports/not_judgeable route nothing; questionable/conflicts each become one flag that
    carries the audit's code + concise summary and points at the full verbatim note in
    validation/audit_annotations.json — never the duplicated paragraph itself."""
    entries = [
        {
            "aspect": "authoritative_outline",
            "status": "supports",
            "code": "authoritative_outline.supports",
            "summary": "fine",
        },
        {
            "aspect": "ridge",
            "status": "not_judgeable",
            "code": "ridge.not_judgeable",
            "summary": "cannot assert a ridge",
        },
        {
            "aspect": "roof_type",
            "status": "conflicts",
            "code": "roof_type.conflicts",
            "summary": "reads flat to the auditor",
        },
        {
            "aspect": "surface",
            "status": "questionable",
            "code": "surface.questionable",
            "summary": "bare majority",
        },
    ]
    flags = provenance.review_flags(cfg, visual_audit=entries)
    assert [f["trigger"] for f in flags] == [
        "visual_audit_questionable",
        "visual_audit_questionable",
    ]
    by_aspect = {f.get("attribute"): f for f in flags}
    assert by_aspect["roof_type"]["reason"].count("reads flat to the auditor") == 1
    assert "[roof_type.conflicts]" in by_aspect["roof_type"]["reason"]
    assert by_aspect["visual_surface_appearance"]["reason"].count("bare majority") == 1
    assert "[surface.questionable]" in by_aspect["visual_surface_appearance"]["reason"]
    for flag in flags:
        assert "not human ground truth" in flag["reason"]
        assert "changes no value" in flag["reason"]
        assert "Full note: validation/audit_annotations.json" in flag["reason"]
        assert len(flag["reason"]) < 300  # a pointer plus a distillation, never the paragraph
