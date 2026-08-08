"""Provenance: who said what, under which licence, and what we are not claiming.

Everything in this module is a *statement about sources and disagreements*. Nothing here
computes an attribute value, and nothing here can change one:

* ``build_conflict`` takes the authoritative value and the image value and returns a flag. It
  returns the authoritative value to nobody and modifies nothing, so there is no code path in
  which an image observation corrects an official field (design sections 5.1, 6.2).
* ``review_flags`` collects ``requires_visual_review`` triggers. A flag is an invitation to
  look, not an edit.
* Manual review is QA only (section 7). The only manual-review helper here is
  ``manual_review_block``, which reports a status and returns a fresh dict every call. There is
  deliberately no setter, no merge and no "apply review" function. Since RTI-004 the pipeline
  can *read* the audit-annotations transcription (:mod:`propx_roofs.audit`) — but only to
  attach report blocks and review flags; there is still no code path from any review input to
  any published attribute, geometry or confidence score.

Not every review trigger is exercised by the selected sample, and that is recorded rather than
engineered around (``docs/study_area_selection.md`` sections 4 and 5):

* ``authoritative_image_conflict`` is **symmetric since the RTI-005 remediation**. The image
  evidence for flat-versus-pitched is ``attributes.roof_form.observe_roof_form``, which can
  indicate ``"pitched"`` (a fully gated ridge detection), ``"flat"`` (positive flatness
  evidence: enough of the roof judgeable and no two-mode brightness partition of ridge
  strength), or abstain — so both disagreement directions can fire:
  authoritative ``Flachdach`` + image ``pitched``, and authoritative ``Schraegdach`` + image
  ``flat``. Abstention is still never a disagreement, and an authoritative ``unknown`` never
  conflicts with anything: the image evidence then simply stands alone. On the selected
  sample no building fires either direction on the evidence the detectors actually produce
  (vie-swv-008 — the reconnaissance flat-vs-``Schraegdach`` hypothesis — measures a two-moded
  interior that supports neither class, so the observation honestly abstains and its full
  evidence is published). Neither detector nor threshold is adjusted to force the flag on.
* ``slope_mean_above_review_threshold`` has **no exemplar at all**: both ``SLOPE_MEAN > 60``
  records in the study bbox failed the interior/IoU filter.

Both mechanisms therefore stay in the pipeline and are tested **synthetically**, on constructed
evidence that genuinely disagrees, so their correctness never depends on a particular real
building behaving a particular way.

The RTI-004 remediation adds evidence-driven triggers (``low_judgeability``,
``weak_cv_agreement``, ``withheld_attributes``) fed by measured quantities computed for every
building, and ``visual_audit_questionable``, which routes the machine-readable transcription of
the model-assisted visual audit (``validation/audit_annotations.json``, see
:mod:`propx_roofs.audit`) into the same review stream. Audit content can add flags and a
``manual_review`` report block; it cannot touch a value, a geometry or a confidence score —
there is still no code path from any review input to any published value.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from . import __version__
from .config import ATTRIBUTION, LICENCE, Config, algorithm_parameters_hash
from .types import JoinEvidence

# All WFS layers come from this endpoint (design section 1.1). The imagery endpoint is a WMTS
# template and is read from the study-area config instead of being repeated here.
WFS_ENDPOINT = "https://data.wien.gv.at/daten/geo"

# Verbatim from design section 8. Repeated in the README and in every output document.
CONFIDENCE_NOTE = (
    "Heuristic reliability indicators, not calibrated probabilities. Not validated against "
    "labelled ground truth."
)

# Verbatim from docs/study_area_selection.md section 4 ("Required README caveat, verbatim"),
# en dash included. A test reads the document and asserts this string still appears in it, so
# the two cannot drift apart silently.
SAMPLE_NOTE = (
    "The 8–10 buildings form a purposive sample selected for attribute diversity, source "
    "availability, and visual interpretability. They are not statistically representative of "
    "Vienna's building stock."
)

# The verified 2025 basis (design section 1.5, Amendment A5). Also carried in
# configs/study_area.yaml; the config wins if the two ever differ, and a test checks they agree.
MODEL_BASIS = (
    "2023 laser-scan surface model (DOM) at 7.5 cm; 2025 aerial imagery; 1 m DGM for far shading"
)

REVIEW_STATUS = "requires_visual_review"
NOT_REVIEWED = "not_yet_reviewed"

CONFLICT_DESCRIPTION_TAIL = (
    "The authoritative value is reported unchanged and has not been overridden."
)

# Order matters only for readability of the output; the imagery comes first because it is the
# one source a reader can check with their own eyes.
_SOURCE_ORDER = ("imagery", "roof_record", "building_parts", "typology", "building_info")


@dataclass(frozen=True)
class SourceDescriptor:
    """One layer we actually used, with its licence and attribution attached to it.

    Licence, attribution and (optionally) a terms URL are **per-source properties read from
    each entry in ``configs/study_area.yaml``'s ``sources:`` block** (RTI-019), because that
    is how CC BY works: the obligation travels with the data, not with the repository. Today
    every source happens to be City of Vienna CC BY 4.0, but the architecture must not bake
    that in — a future DEM from a differently-licensed provider is a config edit, not a code
    change. The ``config.LICENCE``/``config.ATTRIBUTION`` constants remain only as documented
    defaults for an entry that omits the fields.
    """

    name: str
    url: str
    accessed: str
    licence: str = LICENCE
    attribution: str = ATTRIBUTION
    terms_url: str | None = None
    layer: str | None = None
    role: str | None = None
    year: int | None = None
    resolution_m: float | None = None
    model_basis: str | None = None
    flight_dates: tuple[str, ...] = ()
    feature_count_citywide: int | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "url": self.url,
            "licence": self.licence,
            "attribution": self.attribution,
            "accessed": self.accessed,
        }
        for key, value in (
            ("terms_url", self.terms_url),
            ("layer", self.layer),
            ("role", self.role),
            ("year", self.year),
            ("resolution_m", self.resolution_m),
            ("model_basis", self.model_basis),
            ("feature_count_citywide", self.feature_count_citywide),
        ):
            if value is not None:
                out[key] = value
        if self.flight_dates:
            out["flight_dates"] = list(self.flight_dates)
        return out


def _accessed(key: str, raw: dict[str, Any]) -> str:
    """Every source must carry the date it was fetched, or we cannot say when it was true."""
    fetched = raw.get("fetched")
    if not fetched:
        raise ValueError(f"source {key!r} in study_area.yaml has no 'fetched' date")
    return str(fetched)


def _descriptor(key: str, raw: dict[str, Any]) -> SourceDescriptor:
    layer = raw.get("layer")
    name = raw.get("name")
    # "PV-Potenzial 2025 (ogdwien:ANLAGENLEISTUNG2025OGD)" — the human name and the layer
    # identifier together, as in the design section 8 sketch. Several layers have no human
    # name in the config, and the layer identifier alone is the honest label for those.
    label = f"{name} ({layer})" if name and layer else str(name or layer or key)

    flights = tuple(str(d) for d in raw.get("flight_dates", ()))
    year = int(flights[0][:4]) if flights else None
    model_basis = raw.get("model_basis")
    if key == "roof_record" and not model_basis:
        model_basis = MODEL_BASIS

    return SourceDescriptor(
        name=label,
        # The imagery is a WMTS template; every vector layer is served from the one WFS
        # endpoint, so the layer name is what identifies it.
        url=str(raw.get("endpoint") or WFS_ENDPOINT),
        accessed=_accessed(key, raw),
        # Per-source (RTI-019): the config entry's own licence/attribution/terms_url, with the
        # module constants only as documented defaults for entries that omit them.
        licence=str(raw.get("licence") or LICENCE),
        attribution=str(raw.get("attribution") or ATTRIBUTION),
        terms_url=str(raw["terms_url"]) if raw.get("terms_url") else None,
        layer=layer,
        role=raw.get("role"),
        year=year,
        resolution_m=raw.get("native_gsd_m"),
        model_basis=model_basis,
        flight_dates=flights,
        feature_count_citywide=raw.get("feature_count_citywide"),
    )


def source_descriptors(cfg: Config) -> tuple[SourceDescriptor, ...]:
    """Descriptors for every layer named in ``configs/study_area.yaml``'s ``sources:`` block.

    Read from the config rather than duplicated here, so adding a source is a config edit that
    changes ``config_hash`` and therefore shows up in the output.
    """
    raw_sources: dict[str, Any] = dict(cfg.study_area.sources or {})
    ordered = [k for k in _SOURCE_ORDER if k in raw_sources]
    ordered += [k for k in raw_sources if k not in _SOURCE_ORDER]
    return tuple(_descriptor(key, dict(raw_sources[key])) for key in ordered)


def manual_review_block(
    status: str = NOT_REVIEWED, reviewer: str | None = None, n: int | None = None
) -> dict[str, Any]:
    """Read-only manual-review report. Returns a new dict; nothing here writes anywhere.

    A filled review file may change this block's *report* of who looked, and nothing else. No
    attribute, no polygon and no confidence score is reachable from here (section 7).
    """
    return {"status": status, "reviewer": reviewer, "n": n}


# The dependencies whose versions decide numeric output, recorded per run (RTI-001/RTI-020).
# Distribution names as they appear on PyPI; requests is absent deliberately — the offline run
# never imports it.
TRACKED_DEPENDENCIES = (
    "numpy",
    "opencv-python-headless",
    "shapely",
    "pyproj",
    "pillow",
    "jsonschema",
    "PyYAML",
)

_GIT_TIMEOUT_S = 10


def schema_version() -> str:
    """The output-contract version, read from the **packaged** schema, never hardcoded here.

    The value lives in the schema JSON as the ``const`` of ``run.schema_version``, so the
    schema both declares the version and refuses a document stamped with any other one; this
    helper simply reports the single source of truth.
    """
    from .schema import load_schema

    return str(load_schema()["$defs"]["run"]["properties"]["schema_version"]["const"])


def git_provenance() -> dict[str, Any]:
    """The commit this code ran as: ``{"commit": sha, "dirty": bool}`` — or honest nulls.

    Captured with ``git`` at run time, against the repository containing this file. An
    installed wheel outside any checkout has no git history, and that is recorded as
    ``commit: null`` with a ``note`` saying why — never a crash, and never a fabricated SHA.
    A dirty working tree is recorded as ``dirty: true`` because the commit alone then does not
    identify the code that produced the output.
    """
    where = Path(__file__).resolve().parent

    def _git(*args: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ("git", "-C", str(where), *args),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=True,
        ).stdout.strip()

    try:
        commit = _git("rev-parse", "HEAD")
        dirty = bool(_git("status", "--porcelain"))
    except FileNotFoundError:
        return {"commit": None, "dirty": None, "note": "git executable not available"}
    except subprocess.CalledProcessError:
        return {
            "commit": None,
            "dirty": None,
            "note": f"not a git checkout: {where} (installed package?)",
        }
    except (subprocess.SubprocessError, OSError) as error:
        return {"commit": None, "dirty": None, "note": f"git failed: {error}"}
    return {"commit": commit, "dirty": dirty}


def runtime_provenance() -> dict[str, Any]:
    """Interpreter and platform, because byte-identical output is only claimed per-runtime."""
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }


def dependency_versions() -> dict[str, str | None]:
    """Installed versions of the dependencies that decide numeric output.

    ``None`` for a distribution that cannot be resolved — possible in exotic vendored
    environments — rather than a guess or an import-time crash.
    """
    versions: dict[str, str | None] = {}
    for name in TRACKED_DEPENDENCIES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


# Verification verdicts per (resolved manifest path, manifest sha256), so that run_block does
# not re-decode every tile on each call within one process. Keyed by content: a manifest edit
# forces a fresh verification. The verdict is always the result of an actual cache_build.verify
# run in this process - never assumed, never written by anything else.
_CACHE_VERIFY_MEMO: dict[tuple[str, str], bool] = {}


def cache_provenance(cfg: Config) -> dict[str, Any]:
    """The ``run.cache`` object (RTI-011): which cache the run read, and whether it verified.

    ``manifest_sha256`` is the hash of the cache manifest itself — the manifest in turn hashes
    every raw input file, so this one value pins the entire input state of the run.
    ``verified`` reports the outcome of actually running ``cache_build.verify`` against that
    cache (memoised per manifest content within the process); it is a measurement, not an
    assertion. A missing manifest is recorded as an honest null with a note, never invented.
    """
    manifest_path = cfg.study_area.cache_dir / "manifest.json"
    if not manifest_path.is_file():
        return {
            "manifest_sha256": None,
            "verified": False,
            "note": f"no cache manifest at {manifest_path}",
        }
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    key = (str(manifest_path.resolve()), digest)
    if key not in _CACHE_VERIFY_MEMO:
        from . import cache_build

        _CACHE_VERIFY_MEMO[key] = not cache_build.verify(cfg, quiet=True)
    return {"manifest_sha256": digest, "verified": _CACHE_VERIFY_MEMO[key]}


def run_block(
    cfg: Config,
    generated_at: datetime | date | str,
    *,
    pipeline_version: str = __version__,
) -> dict[str, Any]:
    """The ``run`` object of ``roof_attributes.json`` (design section 8).

    Beyond the two parameter hashes, every run records what they cannot see: the git commit
    (or an explicit null), the interpreter/platform, and the versions of the numeric
    dependencies. Together these are what a reader needs to judge whether a byte-identical
    reproduction is even expected, or only a semantic one.
    """
    stamp = generated_at if isinstance(generated_at, str) else generated_at.isoformat()
    area = cfg.study_area
    return {
        "generated_at": stamp,
        "pipeline_version": pipeline_version,
        "schema_version": schema_version(),
        "config_hash": cfg.config_hash,
        # Not a source-code hash: identical parameter values with changed code hash the same.
        "algorithm_parameters_hash": algorithm_parameters_hash(),
        "git": git_provenance(),
        "runtime": runtime_provenance(),
        "dependencies": dependency_versions(),
        "cache": cache_provenance(cfg),
        "study_area": {
            "name": area.name,
            "label": area.label,
            "bbox_wgs84": list(area.bbox_wgs84),
            "crs_metric": area.crs_metric,
            "imagery_zoom": area.imagery_zoom,
        },
        "sources": [s.as_dict() for s in source_descriptors(cfg)],
        "confidence_note": CONFIDENCE_NOTE,
        "sample_note": SAMPLE_NOTE,
        "manual_review": manual_review_block(),
    }


def _comparable(value: Any) -> Any:
    """Case- and whitespace-insensitive for strings; identity otherwise.

    ``"Flat"`` and ``"flat"`` are not a disagreement worth a reviewer's time, and normalising
    here keeps that judgement in one place instead of at every call site.
    """
    return value.strip().casefold() if isinstance(value, str) else value


def build_conflict(
    authoritative_value: Any,
    image_value: Any,
    description: str | None = None,
) -> dict[str, Any] | None:
    """A conflict block when the authoritative class and the image evidence disagree.

    Returns ``None`` when they agree, and also when ``image_value is None``: the image stage
    abstaining is not a disagreement, and treating it as one would flag every shadowed roof.

    The authoritative value is never modified, never returned as a "corrected" value, and never
    replaced by the image value. All this function can do is describe the disagreement
    (sections 5.1, 6.2).
    """
    if image_value is None:
        return None
    if _comparable(authoritative_value) == _comparable(image_value):
        return None
    if not description:
        description = (
            f"Authoritative value indicates {authoritative_value!r}; image evidence indicates "
            f"{image_value!r}. {CONFLICT_DESCRIPTION_TAIL}"
        )
    elif CONFLICT_DESCRIPTION_TAIL not in description:
        description = f"{description.rstrip()} {CONFLICT_DESCRIPTION_TAIL}"
    return {"flag": True, "status": REVIEW_STATUS, "description": description}


def _flag(trigger: str, reason: str, attribute: str | None = None) -> dict[str, Any]:
    out = {"status": REVIEW_STATUS, "trigger": trigger, "reason": reason}
    if attribute:
        out["attribute"] = attribute
    return out


def review_flags(
    cfg: Config,
    *,
    roof_type_conflict: dict[str, Any] | None = None,
    slope_mean_deg: float | None = None,
    join: JoinEvidence | None = None,
    boundary_alignment_warning: dict[str, Any] | None = None,
    ambiguous_enrichment: Sequence[str] = (),
    multiple_point_candidates: str | None = None,
    processing_errors: Sequence[tuple[str, str]] = (),
    image_quality: Mapping[str, Any] | None = None,
    cv_agreement: Mapping[str, Any] | None = None,
    withheld_attributes: Sequence[str] = (),
    visual_audit: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """The ``requires_visual_review`` triggers for one building, each with a reason.

    Eleven triggers. The first four, in the order a reader should weigh them:

    (a) an authoritative-vs-image conflict — the better-motivated trigger, fired only when the
        implemented evidence rule genuinely disagrees. Symmetric since RTI-005: the roof-form
        observation can indicate flat as well as pitched, so both disagreement directions are
        reachable; abstention is never a disagreement. See the module docstring;
    (b) ``SLOPE_MEAN`` above ``roof_type.slope_review_above_deg`` — unusual for a whole-roof
        mean but not impossible, and no selected building exercises it;
    (c) an ambiguous join — the match itself is uncertain, so every attribute drawn through it
        inherits that uncertainty;
    (d) a fired ``boundary_alignment_warning`` — the CV candidate boundary diverges materially
        from the authoritative outline. That warning already carries a
        ``requires_visual_review`` status of its own, so routing it here is what makes the
        record's single ``review_flags`` array the honest answer to "what needs a human?".
        Without it a reader scanning only ``review_flags`` would miss a building the pipeline
        had already marked for inspection.

    And three added by the RTI-012/013/027 remediation, each carried as pre-built reasons
    because the measurement behind them lives in the pipeline, not here:

    (e) ``ambiguous_enrichment`` — a polygon-enrichment value (GEBAEUDETYPOGD) was published
        although a second candidate polygon covers a similar share of the roof. One entry per
        reason in ``ambiguous_enrichment``; a value *withheld* below the overlap floor is not
        flagged here, because nothing questionable was published and the shares are recorded
        in its ``source_detail``;
    (f) ``multiple_point_candidates`` — several GEBAEUDEINFOOGD points fall on this roof
        outline and the nearest-point rule's choice is either a near-tie or the candidates
        materially disagree; the chosen point's values are published and this flag says the
        choice needs eyes;
    (g) ``processing_error`` — an unexpected exception in one attribute's image path was
        degraded to an abstention instead of aborting the run. One entry per
        ``(attribute, reason)`` pair. The abstention is a report about the pipeline, never a
        finding about the roof.

    And four added by the RTI-004 remediation, each fed by a measured quantity computed for
    every building (thresholds under ``review:`` in ``configs/pipeline.yaml``, with their
    rationale written at the value; no building id appears in any of them):

    (h) ``low_judgeability`` — the adaptive shadow/judgeability measurements say a material
        share of this roof could not be read (``image_quality`` carries ``judgeable_fraction``
        and ``shadow_fraction``); every whole-roof statement in the record rests on less
        evidence than its wording suggests. Fired once per building, naming both measurements;
    (i) ``weak_cv_agreement`` — a CV candidate exists but agrees with the authoritative
        outline below ``review.cv_iou_review_floor``, or the two disagree topologically
        (interior-ring counts). ``cv_agreement`` carries ``iou`` and ``topology_mismatch``;
    (j) ``withheld_attributes`` — at least ``review.withheld_attributes_min`` image-derived
        attributes abstained on this building: a pattern about how well the imagery serves the
        building, which no single attribute's abstention reports;
    (k) ``visual_audit_questionable`` — the machine-readable transcription of the
        model-assisted visual audit marks an aspect of this building ``questionable`` or
        ``conflicts``. Each entry in ``visual_audit`` carries ``aspect``, ``status``, a
        stable ``code`` and a concise ``summary``; the reason embeds the code and summary and
        points at ``validation/audit_annotations.json`` for the verbatim note, rather than
        duplicating the full paragraph into every record. The audit examined an earlier
        baseline (its ``audit_basis`` is attached to the building's ``manual_review`` block)
        and is model-assisted QA, not human ground truth; the flag routes its doubt, changes
        nothing, and asserts nothing.

    Nothing is rejected on any of these. A flag says "a human should look", not "this is wrong".
    """
    flags: list[dict[str, Any]] = []

    if roof_type_conflict:
        flags.append(
            _flag(
                "authoritative_image_conflict",
                roof_type_conflict.get("description")
                or f"Authoritative class and image evidence disagree. {CONFLICT_DESCRIPTION_TAIL}",
                "roof_type",
            )
        )

    if slope_mean_deg is not None:
        limit = float(cfg.threshold("roof_type", "slope_review_above_deg"))
        if slope_mean_deg > limit:
            flags.append(
                _flag(
                    "slope_mean_above_review_threshold",
                    f"SLOPE_MEAN {slope_mean_deg} deg exceeds the {limit} deg review threshold. "
                    f"Unusual for a whole-roof mean but not impossible (steep mansard, tower, "
                    f"spire); the authoritative value is published unchanged.",
                    "mean_slope_deg",
                )
            )

    if boundary_alignment_warning and boundary_alignment_warning.get("flag"):
        metrics = boundary_alignment_warning.get("metrics") or {}
        triggers = "; ".join(metrics.get("triggers") or []) or "no trigger recorded"
        flags.append(
            _flag(
                "boundary_alignment_warning",
                f"The image-derived candidate boundary differs materially from the "
                f"authoritative outline ({triggers}). Possible causes include segmentation "
                f"error, shadow or occlusion, roof overhang, or a difference in epoch between "
                f"imagery and the roof record. This does not indicate that the authoritative "
                f"data is incorrect or outdated.",
            )
        )

    if join is not None and join.ambiguous:
        reasons = "; ".join(join.ambiguity_reasons) or "no reason recorded"
        flags.append(
            _flag(
                "ambiguous_join",
                f"FMZK cross-check match is ambiguous ({reasons}). Thresholds behind this are "
                f"documented heuristics, not calibrated accuracy values.",
            )
        )

    for reason in ambiguous_enrichment:
        flags.append(
            _flag(
                "ambiguous_enrichment",
                f"{reason} The margin behind this is a documented heuristic, not a calibrated "
                f"accuracy value; the published value comes from the best candidate and every "
                f"candidate share is recorded in source_detail.",
            )
        )

    if multiple_point_candidates:
        flags.append(
            _flag(
                "multiple_point_candidates",
                f"{multiple_point_candidates} The chosen point's values are published; the "
                f"candidates, their distances and the selection rule are recorded in "
                f"source_detail so the choice is auditable.",
            )
        )

    for attribute, reason in processing_errors:
        flags.append(
            _flag(
                "processing_error",
                f"An unexpected error in this attribute's image processing path was degraded "
                f"to an abstention rather than aborting the run: {reason}. The null is a "
                f"report about the pipeline, not a finding about the roof.",
                attribute,
            )
        )

    if image_quality is not None:
        judgeable = image_quality.get("judgeable_fraction")
        shadow = image_quality.get("shadow_fraction")
        judgeable_min = float(cfg.threshold("review", "judgeable_fraction_review"))
        shadow_max = float(cfg.threshold("review", "shadow_fraction_review"))
        reasons = []
        if judgeable is not None and float(judgeable) < judgeable_min:
            reasons.append(
                f"only {float(judgeable):.1%} of the roof outline is judgeable (unshadowed, "
                f"with imagery), below the {judgeable_min:.0%} review threshold"
            )
        if shadow is not None and float(shadow) > shadow_max:
            reasons.append(
                f"{float(shadow):.1%} of the roof outline is shadowed or unimaged by the "
                f"adaptive measurement, above the {shadow_max:.0%} review threshold"
            )
        if reasons:
            flags.append(
                _flag(
                    "low_judgeability",
                    f"{'; '.join(reasons)}. Every whole-roof image statement in this record "
                    f"rests on the readable share only; the thresholds are documented "
                    f"heuristics from configs/pipeline.yaml (review:), not calibrated figures.",
                )
            )

    if cv_agreement is not None:
        iou = cv_agreement.get("iou")
        floor = float(cfg.threshold("review", "cv_iou_review_floor"))
        topology = cv_agreement.get("topology_mismatch") or {}
        reasons = []
        if iou is not None and float(iou) < floor:
            reasons.append(
                f"the CV candidate agrees with the authoritative outline at IoU "
                f"{float(iou):.4f}, below the {floor:.2f} review floor - divergence beyond "
                f"what the eave/parapet ambiguity of this imagery explains"
            )
        if topology.get("flag"):
            reasons.append(
                "the CV candidate and the authoritative outline disagree about interior-ring "
                "topology (see delineation.topology_mismatch): the two estimates disagree "
                "about the roof's structure, not merely its edge"
            )
        if reasons:
            flags.append(
                _flag(
                    "weak_cv_agreement",
                    f"{'; '.join(reasons)}. Agreement between related estimates, never "
                    f"accuracy: this says a human should look at which estimate wandered, not "
                    f"that either is wrong.",
                )
            )

    withheld_min = int(cfg.threshold("review", "withheld_attributes_min"))
    if len(withheld_attributes) >= withheld_min:
        flags.append(
            _flag(
                "withheld_attributes",
                f"{len(withheld_attributes)} image-derived attributes abstained on this "
                f"building ({', '.join(withheld_attributes)}), at or above the review "
                f"threshold of {withheld_min}: the imagery serves this building poorly and a "
                f"human should judge what it can actually support. Each abstention remains an "
                f"abstention, never a negative.",
            )
        )

    _AUDIT_ASPECT_ATTRIBUTE = {
        "roof_type": "roof_type",
        "ridge": "ridge_orientation_deg",
        "surface": "visual_surface_appearance",
    }
    for entry in visual_audit:
        status = entry.get("status")
        if status not in ("questionable", "conflicts"):
            continue
        aspect = str(entry.get("aspect"))
        code = entry.get("code") or f"{aspect}.{status}"
        flags.append(
            _flag(
                "visual_audit_questionable",
                f"[{code}] {entry.get('summary')} (Model-assisted baseline QA, not human "
                f"ground truth; changes no value. Full note: "
                f"validation/audit_annotations.json.)",
                _AUDIT_ASPECT_ATTRIBUTE.get(aspect),
            )
        )

    return flags


def attribution_lines(sources: Iterable[SourceDescriptor]) -> Sequence[str]:
    """One attribution line per distinct licence/attribution pair, for the README and overlays."""
    seen: dict[tuple[str, str], None] = {}
    for src in sources:
        seen.setdefault((src.licence, src.attribution), None)
    return [f"{attribution} ({licence})" for licence, attribution in seen]
