"""Audit ingestion and release checking: review may describe, never edit.

Three properties carry this module:

* the committed ``validation/audit_annotations.json`` is a **faithful transcription** of
  ``validation/visual_audit.json`` — every note appears verbatim in the source audit, every
  status comes from the recorded mapping applied to the recorded source assessment, and the
  audit basis names the earlier baseline the reviewers actually saw. Each entry additionally
  carries a derived ``code`` and a concise ``summary`` — the slim form the output document
  embeds instead of duplicating the verbatim paragraphs per building;
* attaching annotations to a run changes report blocks and review flags and **nothing else**:
  the attribute values, geometries and confidence scores of an annotated run are byte-identical
  to a run with annotations disabled;
* ``release-check`` passes only on artifacts that byte-match their own manifest, and reports
  the audit binding as ``baseline_only`` rather than pretending an audit of earlier bytes
  covers fresh ones. A tampered overlay byte or a stale manifest fails. Strict mode
  (``require_current_audit=True``) additionally exits 3 — consistent but awaiting review —
  until a human review of exactly the current bytes is recorded via :func:`audit.record_review`
  (which these tests only ever run against temporary copies: recording a review that did not
  happen is the one thing this machinery exists to make impossible to do silently).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from propx_roofs import audit, config, pipeline

REPO = Path(__file__).resolve().parents[1]
ANNOTATIONS_PATH = REPO / "validation" / "audit_annotations.json"
VISUAL_AUDIT_PATH = REPO / "validation" / "visual_audit.json"

GENERATED_AT = "2026-08-06T00:00:00+00:00"


@pytest.fixture(scope="module")
def cfg() -> config.Config:
    return config.load()


@pytest.fixture(scope="module")
def annotations() -> dict[str, Any]:
    return audit.load_annotations(ANNOTATIONS_PATH)


@pytest.fixture(scope="module")
def release_run(cfg, tmp_path_factory) -> Path:
    """One real run with overlays, shared read-only by the release-check tests."""
    out = tmp_path_factory.mktemp("release_run")
    pipeline.run(cfg, generated_at=GENERATED_AT, output_dir=out)
    return out


# --- the transcription is faithful ------------------------------------------------------------


def test_the_committed_annotations_load_and_cover_the_whole_sample(annotations) -> None:
    assert len(annotations["buildings"]) == 10
    for building_id, entries in annotations["buildings"].items():
        assert building_id.startswith("vie-swv-")
        assert {entry["aspect"] for entry in entries} == set(audit.ASPECTS)


def test_every_note_is_verbatim_from_the_source_audit(annotations) -> None:
    """No invented, paraphrased or upgraded content: each note must appear character for
    character in validation/visual_audit.json."""
    source_text = VISUAL_AUDIT_PATH.read_text(encoding="utf-8")
    for entries in annotations["buildings"].values():
        for entry in entries:
            assert json.dumps(entry["note"], ensure_ascii=False)[1:-1] in source_text, (
                f"note not found verbatim in the source audit: {entry['note'][:60]!r}"
            )


def test_every_status_is_the_recorded_mapping_of_the_recorded_source_assessment(
    annotations,
) -> None:
    """The status may only ever be the documented mapping applied to the verbatim source
    assessment — transcription, not judgement."""
    source = json.loads(VISUAL_AUDIT_PATH.read_text(encoding="utf-8"))
    by_id = {entry["building_id"]: entry for entry in source["buildings"]}
    source_key = {"surface": "surface_class"}
    field_for = {"roof_type": "verdict"}
    for building_id, entries in annotations["buildings"].items():
        for entry in entries:
            aspect = entry["aspect"]
            block = by_id[building_id][source_key.get(aspect, aspect)]
            recorded = block[field_for.get(aspect, "assessment")]
            assert entry["source_assessment"] == recorded
            mapping_key = f"{source_key.get(aspect, aspect)}.{field_for.get(aspect, 'assessment')}"
            mapping = annotations["assessment_mapping"][mapping_key]
            assert entry["status"] == mapping[recorded]


def test_every_annotation_carries_a_derived_code_and_a_concise_summary(annotations) -> None:
    """The slim embedded form is derived, capped and distinct: the code is exactly
    aspect.status (a key, never a second assessment), and the summary is a short distillation,
    never a copy of the verbatim note and never longer than the loader's cap."""
    for entries in annotations["buildings"].values():
        for entry in entries:
            assert entry["code"] == f"{entry['aspect']}.{entry['status']}"
            summary = entry["summary"]
            assert summary.strip()
            assert len(summary) <= audit.SUMMARY_MAX_CHARS
            assert summary != entry["note"]


def test_the_audit_basis_names_both_the_baseline_and_current_human_review(annotations) -> None:
    source = json.loads(VISUAL_AUDIT_PATH.read_text(encoding="utf-8"))
    basis = audit.audit_basis(annotations)
    assert basis["audited_document_sha256"] == source["audited_document_sha256"]
    assert basis["baseline_commit"] == source["baseline_commit"]
    assert basis["source_file_sha256"] == audit.sha256_file(VISUAL_AUDIT_PATH)
    # The baseline audit remains part of the trail. The final human review is separately bound
    # to the exact submitted document and every overlay, rather than replacing that history.
    output_dir = REPO / "outputs"
    assert basis["reaudit_of_artifact_sha256"] == audit.sha256_file(
        output_dir / "roof_attributes.json"
    )
    assert basis["reaudit_reviewer"] == "Mohammad Bius"
    assert basis["reaudit_date"] == "2026-08-08"
    expected_overlays = {
        f"overlays/vie-swv-{n:03d}.png": audit.sha256_file(
            output_dir / "overlays" / f"vie-swv-{n:03d}.png"
        )
        for n in range(1, 11)
    }
    assert basis["reaudit_overlays"] == expected_overlays


def test_the_loader_refuses_invented_vocabulary(tmp_path, annotations) -> None:
    """An annotations file cannot smuggle in new statuses, aspects, an undeclared review type
    or a note-free assessment."""

    def broken(mutate) -> Path:
        raw = json.loads(ANNOTATIONS_PATH.read_text(encoding="utf-8"))
        mutate(raw)
        path = tmp_path / "broken.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def set_status(raw):
        raw["buildings"]["vie-swv-001"][0]["status"] = "confirmed_correct"

    def set_aspect(raw):
        raw["buildings"]["vie-swv-001"][0]["aspect"] = "vibes"

    def drop_note(raw):
        raw["buildings"]["vie-swv-001"][0]["note"] = ""

    def drop_basis(raw):
        del raw["audit_basis"]

    def wrong_type(raw):
        raw["review_type"] = "human_ground_truth"

    def wrong_code(raw):
        # A code disagreeing with aspect.status would be a second, unaccountable assessment.
        raw["buildings"]["vie-swv-001"][0]["code"] = "authoritative_outline.supports_strongly"

    def drop_summary(raw):
        raw["buildings"]["vie-swv-001"][0]["summary"] = " "

    def oversized_summary(raw):
        raw["buildings"]["vie-swv-001"][0]["summary"] = "x" * (audit.SUMMARY_MAX_CHARS + 1)

    for mutate in (
        set_status,
        set_aspect,
        drop_note,
        drop_basis,
        wrong_type,
        wrong_code,
        drop_summary,
        oversized_summary,
    ):
        with pytest.raises(audit.AuditError):
            audit.load_annotations(broken(mutate))


# --- attachment is report-only ----------------------------------------------------------------


def test_manual_review_blocks_report_and_derive_nothing(annotations) -> None:
    block = audit.manual_review_for(annotations, "vie-swv-008")
    assert block is not None
    assert block["status"] == audit.BUILDING_REVIEW_STATUS
    statuses = {entry["aspect"]: entry["status"] for entry in block["annotations"]}
    assert statuses["roof_type"] == "conflicts"  # transcribed, not softened
    assert block["audit_basis"]["baseline_commit"] == "c86cbf4"
    # Slim embedding: the verbatim paragraph stays in the annotations file, which the block
    # points at; the embedded entries carry exactly the four slim keys.
    assert block["detail_ref"] == "validation/audit_annotations.json#vie-swv-008"
    for entry in block["annotations"]:
        assert set(entry) == {"aspect", "status", "code", "summary"}
    assert audit.manual_review_for(annotations, "vie-xxx-999") is None

    run_review = audit.run_manual_review(annotations)
    assert run_review["status"] == audit.RUN_REVIEW_STATUS
    assert run_review["reviewer"] is None  # no human sign-off may be claimed
    assert run_review["n"] == 10


def test_annotations_change_reports_and_flags_but_never_values(cfg, release_run) -> None:
    """The load-bearing integrity property: a run without annotations differs from the
    annotated run ONLY in manual_review blocks and visual_audit_questionable flags."""
    annotated = json.loads((release_run / "roof_attributes.json").read_text(encoding="utf-8"))
    bare = pipeline.run(
        cfg, generated_at=GENERATED_AT, write_overlays=False, audit_annotations=None
    )

    assert bare["run"]["manual_review"] == {
        "status": "not_yet_reviewed",
        "reviewer": None,
        "n": None,
    }
    for annotated_building, bare_building in zip(
        annotated["buildings"], bare["buildings"], strict=True
    ):
        assert "manual_review" not in bare_building
        stripped = {k: v for k, v in annotated_building.items() if k != "manual_review"}
        stripped["review_flags"] = [
            flag
            for flag in stripped["review_flags"]
            if flag["trigger"] != "visual_audit_questionable"
        ]
        stripped["overlay"] = None  # the bare run wrote no overlays
        assert stripped == bare_building


def test_a_broken_annotations_file_fails_the_run_loudly(cfg, tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(pipeline.PipelineError):
        pipeline.run(cfg, generated_at=GENERATED_AT, audit_annotations=bad)


# --- release-check (RTI-016) ------------------------------------------------------------------


def test_consistent_artifacts_pass_with_a_baseline_only_audit_warning(
    cfg, release_run
) -> None:
    report = audit.release_check(cfg, release_run)
    assert report.problems == []
    assert report.exit_code == 0
    assert report.audit_binding == "baseline_only"
    warning = "\n".join(report.warnings)
    assert "baseline_only" in warning
    assert "ce9e792405da36e013af3c5cfbc4d1fab48f1e2a4ffa0dc499ea43db736e34b1" in warning
    assert "c86cbf4" in warning


def test_a_tampered_overlay_byte_fails(cfg, release_run, tmp_path) -> None:
    tampered = tmp_path / "tampered"
    shutil.copytree(release_run, tampered)
    overlay = tampered / "overlays" / "vie-swv-001.png"
    overlay.write_bytes(overlay.read_bytes() + b"\x00")
    report = audit.release_check(cfg, tampered)
    assert report.exit_code == 1
    assert any(
        "vie-swv-001.png" in problem and "mismatch" in problem for problem in report.problems
    )


def test_an_unmanifested_file_in_overlays_fails(cfg, release_run, tmp_path) -> None:
    """Disk -> manifest: a hand-placed overlay the manifest never fingerprinted must fail.

    The atomic swap cannot produce this state; only post-publication tampering can. An earlier
    revision checked manifest -> disk only, so an extra file passed while the docstring claimed
    otherwise.
    """
    padded = tmp_path / "padded"
    shutil.copytree(release_run, padded)
    extra = padded / "overlays" / "vie-swv-999.png"
    shutil.copy(padded / "overlays" / "vie-swv-001.png", extra)
    report = audit.release_check(cfg, padded)
    assert report.exit_code == 1
    assert any(
        "vie-swv-999.png" in problem and "not fingerprinted" in problem
        for problem in report.problems
    )


def test_a_stale_manifest_fails(cfg, release_run, tmp_path) -> None:
    stale = tmp_path / "stale"
    shutil.copytree(release_run, stale)
    manifest = json.loads((stale / "artifact_manifest.json").read_text(encoding="utf-8"))
    manifest["generated_at"] = "1999-01-01T00:00:00+00:00"
    (stale / "artifact_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    report = audit.release_check(cfg, stale)
    assert report.exit_code == 1
    assert any("generated_at" in problem for problem in report.problems)


def test_an_edited_document_fails_against_its_manifest(cfg, release_run, tmp_path) -> None:
    edited = tmp_path / "edited"
    shutil.copytree(release_run, edited)
    path = edited / "roof_attributes.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace("gravel_or_bitumen_like", "polished_marble"),
        encoding="utf-8",
    )
    report = audit.release_check(cfg, edited)
    assert report.exit_code == 1


def test_a_recorded_reaudit_of_the_current_artifact_binds_it(
    cfg, release_run, tmp_path
) -> None:
    """The forward path designed now: once a human re-audit records this artifact's sha256,
    the binding is reported as current instead of baseline_only."""
    current_sha = audit.sha256_file(release_run / "roof_attributes.json")
    raw = json.loads(ANNOTATIONS_PATH.read_text(encoding="utf-8"))
    raw["audit_basis"]["reaudit_of_artifact_sha256"] = current_sha
    reaudited = tmp_path / "reaudited.json"
    reaudited.write_text(json.dumps(raw), encoding="utf-8")
    report = audit.release_check(cfg, release_run, annotations_path=reaudited)
    assert report.problems == []
    assert report.audit_binding == "current"


def test_release_check_without_annotations_reports_no_binding(cfg, release_run) -> None:
    missing = Path("/nonexistent/annotations.json")
    report = audit.release_check(cfg, release_run, annotations_path=missing)
    # An unreadable explicit file is a problem, not a silent "none".
    assert report.exit_code == 1


# --- strict final-release mode + record-review (temp copies ONLY) -----------------------------


def _signed_copy(run_dir: Path, tmp_path: Path) -> Path:
    """A COPY of the committed annotations with a synthetic review record for ``run_dir``.

    record_review is only ever pointed at temporary copies here: writing a review record
    into the real validation/audit_annotations.json would fabricate a human review that
    never happened, which is the exact lie this machinery exists to prevent.
    """
    signed = tmp_path / "signed_annotations.json"
    shutil.copy(ANNOTATIONS_PATH, signed)
    audit.record_review(run_dir, signed, "Synthetic Test Reviewer", "2026-08-08")
    return signed


def _unsigned_copy(tmp_path: Path) -> Path:
    """Return a temporary baseline-only copy, regardless of the repository's release state."""
    raw = json.loads(ANNOTATIONS_PATH.read_text(encoding="utf-8"))
    basis = raw["audit_basis"]
    basis["reaudit_of_artifact_sha256"] = None
    for key in ("reaudit_overlays", "reaudit_reviewer", "reaudit_date"):
        basis.pop(key, None)
    unsigned = tmp_path / "unsigned_annotations.json"
    unsigned.write_text(json.dumps(raw), encoding="utf-8")
    return unsigned


def test_strict_mode_exits_3_awaiting_review_on_the_unsigned_state(
    cfg, release_run, tmp_path
) -> None:
    """The honest final-release gate: artifacts consistent (not exit 1), audit binding still
    baseline-only, so strict mode reports 'awaiting current-artifact review' as exit 3."""
    unsigned = _unsigned_copy(tmp_path)
    report = audit.release_check(
        cfg,
        release_run,
        annotations_path=unsigned,
        require_current_audit=True,
    )
    assert report.problems == []
    assert report.ok  # NOT an artifact inconsistency...
    assert report.exit_code == 3  # ...but not releasable either
    assert report.audit_binding == "baseline_only"
    text = "\n".join(report.awaiting_review)
    assert "reaudit_of_artifact_sha256 is null" in text
    assert "AWAITING" in text
    assert "record-review" in text
    # Non-strict on the same directory stays a diagnostic pass, unchanged.
    assert audit.release_check(cfg, release_run, annotations_path=unsigned).exit_code == 0


def test_a_recorded_review_of_the_current_artifacts_passes_strict_mode(
    cfg, release_run, tmp_path
) -> None:
    signed = _signed_copy(release_run, tmp_path)
    basis = json.loads(signed.read_text(encoding="utf-8"))["audit_basis"]
    assert basis["reaudit_of_artifact_sha256"] == audit.sha256_file(
        release_run / "roof_attributes.json"
    )
    assert set(basis["reaudit_overlays"]) == {
        f"overlays/vie-swv-{n:03d}.png" for n in range(1, 11)
    }
    for name, digest in basis["reaudit_overlays"].items():
        assert digest == audit.sha256_file(release_run / name)
    assert basis["reaudit_reviewer"] == "Synthetic Test Reviewer"
    assert basis["reaudit_date"] == "2026-08-08"

    report = audit.release_check(
        cfg, release_run, annotations_path=signed, require_current_audit=True
    )
    assert report.problems == []
    assert report.awaiting_review == []
    assert report.exit_code == 0
    assert report.audit_binding == "current"


def test_a_tampered_overlay_after_sign_off_fails_with_1(cfg, release_run, tmp_path) -> None:
    """Post-sign-off tampering is an artifact inconsistency (1), never a mere 'awaiting' (3):
    the manifest byte-check catches the changed overlay before the review record is weighed."""
    tampered = tmp_path / "tampered"
    shutil.copytree(release_run, tampered)
    signed = _signed_copy(tampered, tmp_path)
    overlay = tampered / "overlays" / "vie-swv-001.png"
    overlay.write_bytes(overlay.read_bytes() + b"\x00")
    report = audit.release_check(
        cfg, tampered, annotations_path=signed, require_current_audit=True
    )
    assert report.exit_code == 1
    assert any("vie-swv-001.png" in problem for problem in report.problems)


def test_a_review_record_missing_one_overlay_exits_3(cfg, release_run, tmp_path) -> None:
    """A review that never hashed one of the published overlays did not review the release."""
    signed = _signed_copy(release_run, tmp_path)
    raw = json.loads(signed.read_text(encoding="utf-8"))
    del raw["audit_basis"]["reaudit_overlays"]["overlays/vie-swv-005.png"]
    signed.write_text(json.dumps(raw), encoding="utf-8")
    report = audit.release_check(
        cfg, release_run, annotations_path=signed, require_current_audit=True
    )
    assert report.problems == []
    assert report.exit_code == 3
    assert any(
        "vie-swv-005" in gap and "never part of the recorded review" in gap
        for gap in report.awaiting_review
    )


def test_a_review_of_different_artifact_bytes_exits_3(cfg, release_run, tmp_path) -> None:
    signed = _signed_copy(release_run, tmp_path)
    raw = json.loads(signed.read_text(encoding="utf-8"))
    raw["audit_basis"]["reaudit_of_artifact_sha256"] = "b" * 64
    signed.write_text(json.dumps(raw), encoding="utf-8")
    report = audit.release_check(
        cfg, release_run, annotations_path=signed, require_current_audit=True
    )
    assert report.exit_code == 3
    assert any("different bytes" in gap for gap in report.awaiting_review)


def test_record_review_refuses_an_unsigned_or_impossible_record(release_run, tmp_path) -> None:
    signed = tmp_path / "copy.json"
    shutil.copy(ANNOTATIONS_PATH, signed)
    with pytest.raises(audit.AuditError):
        audit.record_review(release_run, signed, "   ")  # unsigned reviews do not exist
    with pytest.raises(audit.AuditError):
        audit.record_review(tmp_path / "no_such_run", signed, "Reviewer")


# --- the human-review checklist ---------------------------------------------------------------


def test_review_checklist_is_deterministic_complete_and_unchecked(release_run) -> None:
    text = audit.review_checklist(release_run)
    assert text == audit.review_checklist(release_run)  # deterministic regeneration
    assert text.count("- [ ]") == 10  # one unchecked box per building...
    assert "[x]" not in text and "[X]" not in text  # ...and the generator ticks none
    assert audit.sha256_file(release_run / "roof_attributes.json") in text
    for n in range(1, 11):
        assert f"vie-swv-{n:03d}" in text
        assert f"overlays/vie-swv-{n:03d}.png" in text
    assert "record-review" in text
    assert "validation/audit_annotations.json" in text
