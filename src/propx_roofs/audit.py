"""Audit-annotation ingestion and release checking. Review can describe; it can never edit.

Two responsibilities, both about tying human-facing QA to machine-readable output without
letting either rewrite the other:

**Annotation ingestion (RTI-004).** ``validation/audit_annotations.json`` is a faithful
mechanical transcription of the model-assisted visual audit in ``validation/visual_audit.json``
— per building, per aspect, a status from a closed vocabulary plus the audit's note verbatim,
under an ``audit_basis`` that names the *earlier* baseline artifact the audit actually examined
(sha256 + commit). When the file is present the pipeline attaches a per-building
``manual_review`` block and derives ``visual_audit_questionable`` review flags for aspects the
audit doubts. The integrity rules are structural, not aspirational:

* the loader validates shape and vocabulary and refuses anything else — an annotations file
  cannot smuggle in new assessment kinds;
* nothing in this module returns an attribute value, a geometry or a confidence, so there is no
  code path from audit content to published content;
* the ``audit_basis`` travels into every block it produces, so no consumer can mistake the
  attached QA for a fresh audit of the artifact it is attached to. The audit is model-assisted
  QA, **not** human ground truth, and every block says so.

**Release checking (RTI-016).** ``release_check`` verifies that the artifacts in an output
directory are exactly the ones the run fingerprinted (``artifact_manifest.json``, byte for
byte), that the manifest is internally consistent with the document, that the document still
validates against the packaged schema, and that the run's recorded input-cache hash matches the
cache actually on disk. It then reports the **audit binding** honestly: the visual audit
examined an earlier baseline, so unless ``audit_basis.reaudit_of_artifact_sha256`` names the
current artifact's sha256, the binding is ``baseline_only`` — a warning, never silently a pass
of the audit onto fresh bytes. Exit code 0 means "artifacts internally consistent" (possibly
with the baseline-only warning); any artifact/manifest/schema/cache inconsistency is nonzero.

**Strict final-release mode.** ``release_check(..., require_current_audit=True)`` closes the
honest gap for a final release: the check then *fails* (exit code 3, distinct from 1 =
inconsistency) unless the annotations file records a completed human review of the CURRENT
artifacts — ``reaudit_of_artifact_sha256`` equal to the artifact's sha256, a
``reaudit_overlays`` map covering every overlay the document names with sha256s matching the
files on disk, a non-empty ``reaudit_reviewer`` and a ``reaudit_date``. The record is written
by :func:`record_review` (CLI ``record-review``), which a human runs **after** genuinely
reviewing; running it asserts that review happened, so nothing in this repository may ever run
it automatically. :func:`review_checklist` generates the compact per-building checklist that
review works through.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, schema
from .config import Config

#: Default location of the transcribed annotations, relative to the checkout.
DEFAULT_ANNOTATIONS_PATH = config.REPO_ROOT / "validation" / "audit_annotations.json"

#: Closed vocabularies. An annotations file using any other word is rejected, so review
#: content cannot invent assessment kinds the schema and the consumers never agreed to.
ASPECTS = (
    "authoritative_outline",
    "cv_candidate",
    "roof_type",
    "ridge",
    "surface",
    "image_quality",
)
STATUSES = ("supports", "questionable", "conflicts", "not_judgeable")

#: The building-level manual_review status; distinct from a human "reviewed" on purpose.
BUILDING_REVIEW_STATUS = "visual_qa_annotated"
#: The run-level manual_review status when annotations are attached. Deliberately not
#: "reviewed": what is attached is model-assisted QA of an EARLIER baseline, and the status
#: must not read as more than that.
RUN_REVIEW_STATUS = "visual_qa_of_baseline_attached"

_AUDIT_BASIS_REQUIRED = (
    "source_file",
    "audited_document_sha256",
    "baseline_commit",
    "reaudit_of_artifact_sha256",
)

#: Hard cap on the embedded per-annotation summary. The verbatim note stays in
#: ``validation/audit_annotations.json``; the output document carries only this distillation
#: plus a ``detail_ref`` pointer, so the cap is what keeps the artifact readable. 140 keeps
#: even the longest derived review-flag reason (code + summary + fixed framing) under 300.
SUMMARY_MAX_CHARS = 140

#: Where the full verbatim audit trail lives, as written into every ``detail_ref``.
ANNOTATIONS_DETAIL_FILE = "validation/audit_annotations.json"


class AuditError(ValueError):
    """The annotations file is unusable. Loud, so bad review input never half-applies."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_annotations_path() -> Path | None:
    """The repo's annotations file, or ``None`` when absent (e.g. an installed wheel)."""
    return DEFAULT_ANNOTATIONS_PATH if DEFAULT_ANNOTATIONS_PATH.is_file() else None


def load_annotations(path: Path | str) -> dict[str, Any]:
    """Parse and validate the annotations file. Raises :class:`AuditError` on anything off.

    Validation is structural: required top-level fields, the closed aspect/status
    vocabularies, verbatim notes present, a ``code``/``summary`` pair per entry (the concise
    machine-readable distillation the output document embeds), and an ``audit_basis`` that
    records which earlier artifact was audited. Content is never reinterpreted — the loader
    will not "fix" a status.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"cannot read audit annotations {path}: {error}") from error

    if raw.get("review_type") != "model_assisted_visual_qa":
        raise AuditError(
            f"{path}: review_type must be 'model_assisted_visual_qa'; this loader refuses to "
            f"attach review content of an undeclared kind"
        )
    basis = raw.get("audit_basis")
    if not isinstance(basis, dict):
        raise AuditError(f"{path}: missing audit_basis object")
    for key in _AUDIT_BASIS_REQUIRED:
        if key not in basis:
            raise AuditError(f"{path}: audit_basis is missing {key!r}")
    buildings = raw.get("buildings")
    if not isinstance(buildings, dict) or not buildings:
        raise AuditError(f"{path}: missing or empty buildings mapping")
    for building_id, entries in buildings.items():
        if not isinstance(entries, list) or not entries:
            raise AuditError(f"{path}: {building_id}: annotations must be a non-empty list")
        for entry in entries:
            aspect, status = entry.get("aspect"), entry.get("status")
            if aspect not in ASPECTS:
                raise AuditError(f"{path}: {building_id}: unknown aspect {aspect!r}")
            if status not in STATUSES:
                raise AuditError(f"{path}: {building_id}: unknown status {status!r}")
            if not str(entry.get("note") or "").strip():
                raise AuditError(
                    f"{path}: {building_id}: {aspect}: an annotation without its verbatim "
                    f"note would be an assessment with no evidence trail"
                )
            if entry.get("code") != f"{aspect}.{status}":
                raise AuditError(
                    f"{path}: {building_id}: {aspect}: code must be '{aspect}.{status}' "
                    f"(got {entry.get('code')!r}); the code is a derived key, never a "
                    f"second assessment"
                )
            summary = str(entry.get("summary") or "").strip()
            if not summary or len(summary) > SUMMARY_MAX_CHARS:
                raise AuditError(
                    f"{path}: {building_id}: {aspect}: summary must be a non-empty "
                    f"distillation of at most {SUMMARY_MAX_CHARS} characters - it is what "
                    f"the output document embeds in place of the verbatim note"
                )
    return raw


def audit_basis(annotations: dict[str, Any]) -> dict[str, Any]:
    """The ``audit_basis`` block as attached to output: a fresh copy, never a live reference."""
    return dict(annotations["audit_basis"])


def building_annotations(
    annotations: dict[str, Any], building_id: str
) -> list[dict[str, Any]]:
    """This building's annotation entries (fresh copies), or an empty list."""
    return [dict(entry) for entry in annotations["buildings"].get(building_id, ())]


def manual_review_for(
    annotations: dict[str, Any], building_id: str
) -> dict[str, Any] | None:
    """The per-building ``manual_review`` block, or ``None`` when the audit never saw it.

    A report, nothing else: the annotations, the closed status vocabulary they use, and the
    basis stating which earlier artifact was examined. No value is derivable from it.

    Embedded annotations are the **slim** form — ``aspect``, ``status``, ``code``,
    ``summary`` — never the verbatim paragraph: duplicating hundreds of words per building
    into the artifact added bytes, not evidence. ``detail_ref`` points at the building's
    entry in the annotations file, where every note remains verbatim and authoritative.
    """
    entries = building_annotations(annotations, building_id)
    if not entries:
        return None
    return {
        "status": BUILDING_REVIEW_STATUS,
        "annotations": [
            {key: entry[key] for key in ("aspect", "status", "code", "summary")}
            for entry in entries
        ],
        "detail_ref": f"{ANNOTATIONS_DETAIL_FILE}#{building_id}",
        "audit_basis": audit_basis(annotations),
    }


def run_manual_review(annotations: dict[str, Any]) -> dict[str, Any]:
    """The run-level ``manual_review`` block when annotations are attached.

    ``reviewer`` stays ``None``: the transcription is model-assisted QA, and naming a reviewer
    would claim a human sign-off that never happened. ``n`` is the number of buildings the
    audit annotated, a count of report blocks rather than of approvals.
    """
    return {
        "status": RUN_REVIEW_STATUS,
        "reviewer": None,
        "n": len(annotations["buildings"]),
        "audit_basis": audit_basis(annotations),
        "note": (
            "Model-assisted visual QA of the EARLIER baseline named in audit_basis is "
            "attached per building under manual_review; it is not human review, not ground "
            "truth, and not an audit of this artifact. No value, geometry or confidence was "
            "or can be changed by it."
        ),
    }


# --------------------------------------------------------------------------------------
# release checking (RTI-016)
# --------------------------------------------------------------------------------------


@dataclass
class ReleaseReport:
    """What ``release-check`` found. ``ok`` means the artifacts are internally consistent.

    ``awaiting_review`` is populated only in strict mode (``require_current_audit=True``):
    the artifacts are consistent, but no completed human review of the CURRENT artifacts is
    recorded. That state has its own exit code, 3, so a release gate can distinguish "fix
    the artifacts" (1) from "a human still has to look" (3).
    """

    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    awaiting_review: list[str] = field(default_factory=list)
    audit_binding: str = "none"

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def exit_code(self) -> int:
        if self.problems:
            return 1
        if self.awaiting_review:
            return 3
        return 0


def _load_json(path: Path, report: ReleaseReport) -> dict[str, Any] | None:
    if not path.is_file():
        report.problems.append(f"missing artifact: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        report.problems.append(f"unparseable artifact {path}: {error}")
        return None


def release_check(
    cfg: Config,
    output_dir: Path | str,
    annotations_path: Path | str | None = None,
    *,
    require_current_audit: bool = False,
) -> ReleaseReport:
    """Verify an output directory against its own manifest, schema, cache and audit basis.

    PASS (exit 0) requires: (a) every file the manifest fingerprints exists with exactly the
    recorded sha256 and no run artifact exists outside the manifest; (b) the manifest is
    internally consistent with the document (generated_at, config_hash, the overlay list);
    (c) the document validates against the packaged schema; (d) the run's recorded
    ``cache.manifest_sha256`` matches the cache manifest on disk. The audit binding is then
    *reported*, never asserted: unless a recorded re-audit names the current artifact's
    sha256, it is ``baseline_only`` — an explicit warning on an otherwise passing check,
    because an audit of earlier bytes does not transfer to these bytes.

    With ``require_current_audit=True`` the baseline-only state stops being a warning and
    becomes exit code 3 ("artifacts consistent but awaiting current-artifact human review"):
    strict mode passes only when ``audit_basis`` records a completed review of exactly these
    bytes — artifact sha256, every named overlay's sha256, a reviewer and a date (see
    :func:`record_review`).
    """
    from .pipeline import RUN_ARTIFACT_JSON, RUN_ARTIFACT_MANIFEST

    output_dir = Path(output_dir)
    report = ReleaseReport()

    document = _load_json(output_dir / RUN_ARTIFACT_JSON, report)
    manifest = _load_json(output_dir / RUN_ARTIFACT_MANIFEST, report)
    if document is None or manifest is None:
        return report

    # (a) byte-for-byte artifact fingerprints.
    files = manifest.get("files")
    if not isinstance(files, dict) or RUN_ARTIFACT_JSON not in files:
        report.problems.append(
            f"manifest does not fingerprint {RUN_ARTIFACT_JSON}; it cannot vouch for this run"
        )
        return report
    for name, recorded in sorted(files.items()):
        artifact = output_dir / name
        if not artifact.is_file():
            report.problems.append(f"manifest names {name} but the file is missing")
            continue
        actual = sha256_file(artifact)
        if actual != recorded:
            report.problems.append(
                f"{name}: sha256 mismatch - manifest records {recorded}, file is {actual}; "
                f"the artifact changed after it was fingerprinted"
            )
    # (b) internal consistency with the document: the manifest must describe exactly the run
    # the document claims - same generation stamp, same config, same overlay set.
    run = document.get("run") or {}
    for key in ("generated_at", "config_hash"):
        if manifest.get(key) != run.get(key):
            report.problems.append(
                f"manifest {key} {manifest.get(key)!r} does not match the document's "
                f"{run.get(key)!r}; the manifest describes a different run"
            )
    expected = sorted(
        [RUN_ARTIFACT_JSON]
        + [b["overlay"] for b in document.get("buildings", []) if b.get("overlay")]
    )
    if sorted(files) != expected:
        report.problems.append(
            f"manifest fingerprints {sorted(files)} but the document names {expected}; "
            f"files exist that the run did not describe, or described files are unfingerprinted"
        )
    # Disk -> manifest, not only manifest -> disk. The atomic swap never publishes a file the
    # manifest does not name, so anything under overlays/ without a fingerprint was placed there
    # by hand after publication - exactly the state a release check exists to refuse. The sweep
    # is confined to overlays/ because the output directory legitimately holds unrelated
    # committed entries (e.g. outputs/_solar_eval/) that are not run artifacts.
    overlay_dir = output_dir / "overlays"
    if overlay_dir.is_dir():
        for stray in sorted(overlay_dir.rglob("*")):
            if stray.is_file() and str(stray.relative_to(output_dir)) not in files:
                report.problems.append(
                    f"{stray.relative_to(output_dir)} exists on disk but is not fingerprinted "
                    f"by the manifest; it is not an artifact of this run"
                )

    # (c) the contract still holds for these bytes.
    try:
        schema.validate(document)
        report.info.append("schema: document validates against the packaged schema")
    except schema.SchemaValidationError as error:
        report.problems.append(f"schema validation failed:\n{error}")

    # (d) the input cache the run recorded is the cache on disk now.
    cache_manifest = cfg.study_area.cache_dir / "manifest.json"
    recorded_cache = (run.get("cache") or {}).get("manifest_sha256")
    if not cache_manifest.is_file():
        report.problems.append(f"cache manifest missing: {cache_manifest}")
    elif recorded_cache != sha256_file(cache_manifest):
        report.problems.append(
            f"run.cache.manifest_sha256 {recorded_cache} does not match the current cache "
            f"manifest {sha256_file(cache_manifest)}; the run's inputs are not the cache on "
            f"disk (stale run or changed cache)"
        )
    else:
        report.info.append("cache: run.cache.manifest_sha256 matches the cache on disk")

    # Audit binding: reported, never upgraded.
    artifact_sha = None
    if (output_dir / RUN_ARTIFACT_JSON).is_file():
        artifact_sha = sha256_file(output_dir / RUN_ARTIFACT_JSON)
        report.info.append(f"current {RUN_ARTIFACT_JSON} sha256: {artifact_sha}")
    if annotations_path is None:
        annotations_path = default_annotations_path()
    if annotations_path is None:
        report.audit_binding = "none"
        report.warnings.append(
            "audit_binding: none - no audit annotations file found; no visual audit is bound "
            "to this artifact at all"
        )
        if require_current_audit:
            report.awaiting_review.append(
                "strict mode: no annotations file exists, so no human review of the current "
                "artifacts can be recorded at all"
            )
        return report
    try:
        annotations = load_annotations(annotations_path)
    except AuditError as error:
        report.problems.append(str(error))
        return report
    basis = audit_basis(annotations)
    reaudit = basis.get("reaudit_of_artifact_sha256")
    if artifact_sha is not None and reaudit == artifact_sha:
        report.audit_binding = "current"
        report.info.append(
            f"audit_binding: current - a recorded re-audit names this artifact "
            f"({artifact_sha})"
        )
    else:
        report.audit_binding = "baseline_only"
        report.warnings.append(
            f"audit_binding: baseline_only - the visual audit examined the EARLIER baseline "
            f"artifact {basis.get('audited_document_sha256')} at commit "
            f"{basis.get('baseline_commit')}, not this artifact"
            + (f" ({artifact_sha})" if artifact_sha else "")
            + ". The audit does not transfer to these bytes; a future human re-audit would "
            "record this artifact's sha256 in audit_basis.reaudit_of_artifact_sha256."
        )
    if require_current_audit and report.ok:
        report.awaiting_review.extend(
            _current_review_gaps(basis, document, output_dir, artifact_sha)
        )
        if report.awaiting_review:
            report.awaiting_review.append(
                "artifacts are internally consistent but AWAITING a completed human review "
                "of the current artifacts: work through validation/final_review_checklist.md "
                "(propx-roofs review-checklist), then record the review with "
                "'propx-roofs record-review --reviewer \"Name\"'"
            )
        else:
            report.info.append(
                f"strict mode: a completed human review of the current artifacts is recorded "
                f"(reviewer {basis.get('reaudit_reviewer')!r}, date "
                f"{basis.get('reaudit_date')!r}, artifact + "
                f"{len(basis.get('reaudit_overlays') or {})} overlay hash(es) match disk)"
            )
    return report


def _current_review_gaps(
    basis: dict[str, Any],
    document: dict[str, Any],
    output_dir: Path,
    artifact_sha: str | None,
) -> list[str]:
    """Why the recorded review does NOT cover the current artifacts; empty when it does.

    Every gap is measured against the bytes on disk: the artifact's sha256, every overlay the
    document names, the reviewer and the date. A recorded hash that no longer matches the file
    is a gap — the review examined other bytes — and an overlay the record never hashed was
    never reviewed.
    """
    gaps: list[str] = []
    reaudit = basis.get("reaudit_of_artifact_sha256")
    if reaudit is None:
        gaps.append(
            "audit_basis.reaudit_of_artifact_sha256 is null: no human review of any final "
            "artifact has been recorded (the visual audit covered the earlier baseline only)"
        )
    elif artifact_sha is not None and reaudit != artifact_sha:
        gaps.append(
            f"audit_basis.reaudit_of_artifact_sha256 {reaudit} does not match the current "
            f"artifact {artifact_sha}: the recorded review examined different bytes"
        )
    overlays = sorted(
        b["overlay"] for b in document.get("buildings", []) if b.get("overlay")
    )
    recorded_overlays = basis.get("reaudit_overlays")
    if not isinstance(recorded_overlays, dict):
        gaps.append(
            "audit_basis.reaudit_overlays is missing: the recorded review does not say which "
            "overlay bytes were examined"
        )
    else:
        for name in overlays:
            recorded = recorded_overlays.get(name)
            if recorded is None:
                gaps.append(
                    f"reaudit_overlays does not cover {name}: that overlay was never part of "
                    f"the recorded review"
                )
                continue
            overlay_path = output_dir / name
            actual = sha256_file(overlay_path) if overlay_path.is_file() else None
            if recorded != actual:
                gaps.append(
                    f"reaudit_overlays[{name!r}] {recorded} does not match the file on disk "
                    f"({actual}): the reviewed overlay is not the published one"
                )
        for name in sorted(set(recorded_overlays) - set(overlays)):
            gaps.append(
                f"reaudit_overlays names {name}, which the document does not: the record "
                f"describes a different artifact set"
            )
    if not str(basis.get("reaudit_reviewer") or "").strip():
        gaps.append("audit_basis.reaudit_reviewer is missing or empty: reviews are signed")
    if not str(basis.get("reaudit_date") or "").strip():
        gaps.append("audit_basis.reaudit_date is missing: reviews are dated")
    return gaps


# --------------------------------------------------------------------------------------
# recording a completed human review, and the checklist that review works through
# --------------------------------------------------------------------------------------


def record_review(
    output_dir: Path | str,
    annotations_path: Path | str,
    reviewer: str,
    review_date: str | None = None,
) -> dict[str, Any]:
    """Record a completed HUMAN review of the current artifacts into ``audit_basis``.

    Computes the sha256 of ``roof_attributes.json`` and of every overlay the document names,
    and writes ``reaudit_of_artifact_sha256`` / ``reaudit_overlays`` / ``reaudit_reviewer`` /
    ``reaudit_date`` into the annotations file. Returns the fields it recorded.

    **Running this asserts that the named human actually reviewed those artifacts.** The
    function computes hashes; it cannot and does not check that anyone looked at anything.
    Nothing in this repository may call it automatically — it exists solely behind the
    deliberate ``propx-roofs record-review`` invocation, after the reviewer has worked
    through the checklist. Recording a review that did not happen would be fabricating
    the exact sign-off this whole module refuses to fake.
    """
    from datetime import date

    output_dir = Path(output_dir)
    annotations_path = Path(annotations_path)
    reviewer = reviewer.strip()
    if not reviewer:
        raise AuditError("record-review requires a non-empty reviewer name: reviews are signed")

    # Validate first so a broken file is refused before it is rewritten.
    load_annotations(annotations_path)

    from .pipeline import RUN_ARTIFACT_JSON

    artifact = output_dir / RUN_ARTIFACT_JSON
    if not artifact.is_file():
        raise AuditError(f"cannot record a review of a missing artifact: {artifact}")
    document = json.loads(artifact.read_text(encoding="utf-8"))
    overlays = sorted(b["overlay"] for b in document.get("buildings", []) if b.get("overlay"))
    overlay_hashes: dict[str, str] = {}
    for name in overlays:
        overlay_path = output_dir / name
        if not overlay_path.is_file():
            raise AuditError(
                f"the document names {name} but the file is missing; a review of artifacts "
                f"that are not all present cannot be recorded"
            )
        overlay_hashes[name] = sha256_file(overlay_path)

    recorded = {
        "reaudit_of_artifact_sha256": sha256_file(artifact),
        "reaudit_overlays": overlay_hashes,
        "reaudit_reviewer": reviewer,
        "reaudit_date": review_date or date.today().isoformat(),
    }
    raw = json.loads(annotations_path.read_text(encoding="utf-8"))
    raw["audit_basis"].update(recorded)
    annotations_path.write_text(
        json.dumps(raw, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return recorded


def review_checklist(output_dir: Path | str) -> str:
    """The human-review checklist for the artifacts in ``output_dir``, as Markdown.

    Deterministic: derived entirely from the published document (plus the artifact's own
    sha256), so regenerating it against unchanged artifacts is byte-identical. One compact
    table row per building with the published values a reviewer must confirm against the
    overlay, one unchecked checkbox per building, and the exact record-review command that
    closes the loop. The generator never fills a checkbox: checking is the human's act.
    """
    from .pipeline import RUN_ARTIFACT_JSON

    output_dir = Path(output_dir)
    artifact = output_dir / RUN_ARTIFACT_JSON
    if not artifact.is_file():
        raise AuditError(f"cannot build a review checklist without {artifact}")
    document = json.loads(artifact.read_text(encoding="utf-8"))
    artifact_sha = sha256_file(artifact)
    run = document.get("run") or {}

    def _value(attributes: dict[str, Any], name: str) -> Any:
        return (attributes.get(name) or {}).get("value")

    rows: list[str] = []
    checkboxes: list[str] = []
    for building in document.get("buildings", []):
        building_id = building["building_id"]
        attributes = building.get("attributes") or {}
        address = _value(attributes, "address") or "-"

        roof_type = str(_value(attributes, "roof_type") or "withheld")
        if (attributes.get("roof_type") or {}).get("conflict"):
            roof_type += " (CONFLICT)"

        area = _value(attributes, "roof_area_m2")
        area_text = f"{area:g}" if area is not None else "withheld"

        ridge = _value(attributes, "ridge_orientation_deg")
        ridge_text = f"{ridge:g} deg" if ridge is not None else "withheld"

        surface = _value(attributes, "visual_surface_appearance")
        if surface is None:
            surface_text = "withheld"
        else:
            quality = (
                ((attributes.get("visual_surface_appearance") or {}).get("image_evidence"))
                or {}
            ).get("quality") or {}
            dominance = quality.get("dominance_share")
            surface_text = (
                f"{surface} ({float(dominance):.0%})" if dominance is not None else str(surface)
            )

        green = _value(attributes, "green_roof")
        green_text = str(green) if green is not None else "withheld"

        triggers = sorted({flag["trigger"] for flag in building.get("review_flags", [])})
        triggers_text = ", ".join(triggers) if triggers else "-"
        overlay = building.get("overlay") or "-"

        rows.append(
            f"| {building_id} | {address} | {roof_type} | {area_text} | {ridge_text} "
            f"| {surface_text} | {green_text} | {triggers_text} | `{overlay}` |"
        )
        checkboxes.append(
            f"- [ ] **{building_id}** — opened `{overlay}`, checked every published value "
            f"in the row above against the imagery, and read this building's review-flag "
            f"triggers"
        )

    lines = [
        "# Final human-review checklist",
        "",
        "Model-assisted QA covered an earlier baseline; **this checklist is the human review "
        "of the current artifacts**. Work through it, then record the review — the strict "
        "release gate (`propx-roofs release-check --require-current-audit`) stays at exit "
        "code 3 until a completed review of exactly these bytes is recorded.",
        "",
        f"- Document: `outputs/{RUN_ARTIFACT_JSON}`",
        f"- Document sha256: `{artifact_sha}`",
        f"- Generated at (cited from the document): `{run.get('generated_at')}`",
        "",
        "## How to review",
        "",
        "1. Open each building's overlay PNG (paths in the table).",
        "2. Check the published values in that building's row against what the imagery "
        "shows, and read its review-flag triggers (full audit notes: "
        f"`{ANNOTATIONS_DETAIL_FILE}`).",
        "3. Tick the building's checkbox. Do not tick what you did not review.",
        "4. When every box is ticked, record the review (this asserts the review happened):",
        "",
        "   ```",
        '   propx-roofs record-review --reviewer "Mohammad"',
        "   ```",
        "",
        "## Buildings",
        "",
        "| building_id | address | roof_type | area m2 | ridge | surface (dominance) "
        "| green_roof | review-flag triggers | overlay |",
        "|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        "## Sign-off",
        "",
        *checkboxes,
        "",
        "Withheld means the pipeline abstained; confirm the abstention is plausible rather "
        "than hunting for a value. Review can describe; it can never edit — nothing in this "
        "checklist changes any published value.",
        "",
    ]
    return "\n".join(lines)
