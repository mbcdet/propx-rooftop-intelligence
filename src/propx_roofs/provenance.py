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
  deliberately no setter, no merge and no "apply review" function. The pipeline does not read or
  apply a manual-review file, so manual review cannot alter any published attribute or geometry.

The two review triggers are not equally exercised by the selected sample, and that is recorded
rather than engineered around (``docs/study_area_selection.md`` sections 4 and 5):

* ``authoritative_image_conflict`` is **one-directional by construction**, which matters more
  than whether the sample happens to exercise it. ``pipeline.image_roof_type`` returns
  ``"pitched"`` when a ridge is detected and ``None`` otherwise — it can never return
  ``"flat"``, because absence of a ridge is abstention, not evidence of flatness. So the only
  reachable pairing is authoritative ``Flachdach`` + image ``pitched``. Authoritative
  ``Schraegdach`` against a flat-looking roof **cannot** fire this flag under any detector
  reading. It also has **no exemplar in the selected sample** on the evidence the detector
  actually produces. vie-swv-008 / OBJECTID 358486 was picked during
  reconnaissance because a manual read of the imagery suggested a predominantly flat roof
  against ``DACHFORM = "Schraegdach"`` at ``SLOPE_MEAN = 42.92``. That remains a **manual
  visual hypothesis**, and it sits in the unreachable direction: 008 is ``Schraegdach``, so no
  ridge reading could have fired the flag. (The detector separately reports a ridge at
  ~65.2 deg, which supports the authoritative class rather than contradicting it.) The
  disagreement between manual interpretation and algorithmic evidence is itself worth
  reporting, but neither the detector nor the threshold is adjusted to force the flag on.
* ``slope_mean_above_review_threshold`` has **no exemplar at all**: both ``SLOPE_MEAN > 60``
  records in the study bbox failed the interior/IoU filter.

Both mechanisms therefore stay in the pipeline and are tested **synthetically**, on constructed
evidence that genuinely disagrees, so their correctness never depends on a particular real
building behaving a particular way.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
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

    Licence and attribution are per-source rather than global because that is how CC BY works:
    the obligation travels with the data, not with the repository.
    """

    name: str
    url: str
    accessed: str
    licence: str = LICENCE
    attribution: str = ATTRIBUTION
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


def run_block(
    cfg: Config,
    generated_at: datetime | date | str,
    *,
    pipeline_version: str = __version__,
) -> dict[str, Any]:
    """The ``run`` object of ``roof_attributes.json`` (design section 8)."""
    stamp = generated_at if isinstance(generated_at, str) else generated_at.isoformat()
    area = cfg.study_area
    return {
        "generated_at": stamp,
        "pipeline_version": pipeline_version,
        "config_hash": cfg.config_hash,
        # Not a source-code hash: identical parameter values with changed code hash the same.
        "algorithm_parameters_hash": algorithm_parameters_hash(),
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
) -> list[dict[str, Any]]:
    """The ``requires_visual_review`` triggers for one building, each with a reason.

    Four triggers, in the order a reader should weigh them:

    (a) an authoritative-vs-image conflict — the better-motivated trigger, fired only when the
        implemented evidence rule genuinely disagrees. One-directional by construction: only
        authoritative ``Flachdach`` + image ``pitched`` can reach it, so for every
        ``Schraegdach`` record it is structurally impossible rather than merely unexercised.
        See the module docstring on vie-swv-008;
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

    return flags


def attribution_lines(sources: Iterable[SourceDescriptor]) -> Sequence[str]:
    """One attribution line per distinct licence/attribution pair, for the README and overlays."""
    seen: dict[tuple[str, str], None] = {}
    for src in sources:
        seen.setdefault((src.licence, src.attribution), None)
    return [f"{attribution} ({licence})" for licence, attribution in seen]
