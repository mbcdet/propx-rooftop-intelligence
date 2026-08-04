"""Configuration loading for the study area, the pinned buildings and pipeline thresholds.

Two files, deliberately separate:

* ``configs/study_area.yaml`` — what we analyse. Fixed by the Phase 2 decision.
* ``configs/pipeline.yaml`` — how we analyse it. Every threshold is a documented heuristic.

Nothing in the pipeline reads a threshold from a literal; if it is a decision, it is here.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STUDY_AREA = REPO_ROOT / "configs" / "study_area.yaml"
DEFAULT_PIPELINE = REPO_ROOT / "configs" / "pipeline.yaml"
DEFAULT_CACHE_ROOT = REPO_ROOT / "data" / "cache"

ATTRIBUTION = "Datenquelle: Stadt Wien - data.wien.gv.at"
LICENCE = "CC BY 4.0"


# --- algorithm-parameter fingerprint ---------------------------------------------------------
#
# config_hash covers configs/study_area.yaml + configs/pipeline.yaml. It cannot cover the ~35
# algorithm-shape constants in segment.SEGMENT_PARAMS and attributes.ATTRIBUTE_PARAMS, several of
# which decide published output. This fingerprint covers those, so the pair
# (config_hash, algorithm_parameters_hash) pins every number the run depended on.
ALGORITHM_PARAMETERS_NAMESPACE = "propx_roofs.algorithm_parameters"
ALGORITHM_PARAMETERS_VERSION = 1
# 16 hex characters = 64 bits, the same width as config_hash so the two read as siblings.
ALGORITHM_PARAMETERS_DIGEST_CHARS = 16


def _canonical_parameter(name: str, value: object) -> str:
    """One parameter value as an exact, format-frozen string.

    ``float.hex()`` rather than ``repr``: it is an exact base-2 rendering of the IEEE-754 double,
    so ``5`` and ``5.0`` collapse to one string and no future change to Python's float repr can
    move the fingerprint. Non-finite values are refused because ``nan != nan`` would make "the
    same parameters" untestable, and ``bool`` is refused so ``True`` cannot become ``1.0``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"algorithm parameter {name!r} must be an int or a float, got {type(value).__name__}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"algorithm parameter {name!r} is not finite: {number!r}")
    return number.hex()


def algorithm_parameters_payload(
    segment_params: Mapping[str, float], attribute_params: Mapping[str, float]
) -> str:
    """The exact bytes :func:`algorithm_parameters_hash` digests, for tests and inspection."""
    payload = {
        "namespace": ALGORITHM_PARAMETERS_NAMESPACE,
        "version": ALGORITHM_PARAMETERS_VERSION,
        "segment": {str(k): _canonical_parameter(str(k), v) for k, v in segment_params.items()},
        "attributes": {
            str(k): _canonical_parameter(str(k), v) for k, v in attribute_params.items()
        },
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def algorithm_parameters_hash(
    segment_params: Mapping[str, float] | None = None,
    attribute_params: Mapping[str, float] | None = None,
) -> str:
    """Fingerprint of the in-code algorithm parameter **values**, as 16 hex characters.

    Covers every name and value in ``segment.SEGMENT_PARAMS`` and ``attributes.ATTRIBUTE_PARAMS``
    — the constants ``config_hash`` cannot see, several of which decide published output
    (``warn_iou_below``, ``solar_internal_texture_min``, ``ridge_plane_contrast_min``,
    ``grabcut_iterations``, ``grabcut_rng_seed``). Adding, removing, renaming or changing any one
    of them changes this value; reordering the dict literals does not.

    What it does NOT cover, and must never be read as covering:

    * **source code.** This is not a code hash. Rewriting a detector, changing an operator order
      or fixing a bug leaves the fingerprint unchanged as long as the parameter values are equal,
      so two runs agreeing here can still differ in output. ``pipeline_version`` speaks about code.
    * **the two YAML config files.** Those are ``config_hash``.
    * **the effective value when the config overrides a default.** An override lives in
      ``config_hash``; this field still reports the in-code default. Only the two hashes together
      determine the effective parameters.
    * constants outside those two dicts. Widening the scope is deliberate: bump
      ``ALGORITHM_PARAMETERS_VERSION``, which is inside the hashed payload, so a v1 and a v2
      fingerprint can never be silently compared.
    """
    if segment_params is None or attribute_params is None:
        # Function-scoped: segment reaches cv2/numpy/shapely through imaging, and config must
        # stay cheap to import (tools/build_cache.py imports it).
        from .attributes import ATTRIBUTE_PARAMS
        from .segment import SEGMENT_PARAMS

        if segment_params is None:
            segment_params = SEGMENT_PARAMS
        if attribute_params is None:
            attribute_params = ATTRIBUTE_PARAMS
    canonical = algorithm_parameters_payload(segment_params, attribute_params)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[
        :ALGORITHM_PARAMETERS_DIGEST_CHARS
    ]


@dataclass(frozen=True)
class PinnedBuilding:
    """One building unit fixed by the Phase 2 decision.

    The authoritative attribute values recorded here are the ones observed at selection time.
    They are **selection metadata**, not pipeline output: the pipeline reads live values from
    the cached source and must not be seeded from this record.
    """

    building_id: str
    objectid: int
    bw_geb_id: int
    adresse: str | None
    selection_reason: str
    visual_class: str | None
    expected_dachform: str | None
    expected_slope_mean_deg: float | None

    @classmethod
    def from_yaml(cls, raw: dict[str, Any]) -> PinnedBuilding:
        return cls(
            building_id=raw["building_id"],
            objectid=int(raw["objectid"]),
            bw_geb_id=int(raw["bw_geb_id"]),
            adresse=raw.get("adresse"),
            selection_reason=raw.get("selection_reason", ""),
            visual_class=raw.get("visual_class"),
            expected_dachform=raw.get("dachform"),
            expected_slope_mean_deg=raw.get("slope_mean_deg"),
        )


@dataclass(frozen=True)
class StudyArea:
    name: str
    label: str
    bbox_wgs84: tuple[float, float, float, float]
    crs_metric: str
    imagery_zoom: int
    sources: dict[str, Any]
    buildings: tuple[PinnedBuilding, ...]

    @property
    def cache_dir(self) -> Path:
        return DEFAULT_CACHE_ROOT / self.name


@dataclass(frozen=True)
class Config:
    study_area: StudyArea
    thresholds: dict[str, Any]
    config_hash: str
    study_area_path: Path
    pipeline_path: Path

    def threshold(self, *path: str) -> Any:
        """Fetch a nested threshold, failing loudly rather than defaulting silently."""
        node: Any = self.thresholds
        for key in path:
            if not isinstance(node, dict) or key not in node:
                raise KeyError(f"missing threshold {'.'.join(path)} in {self.pipeline_path}")
            node = node[key]
        return node


def load(
    study_area_path: Path | str = DEFAULT_STUDY_AREA,
    pipeline_path: Path | str = DEFAULT_PIPELINE,
) -> Config:
    study_area_path = Path(study_area_path)
    pipeline_path = Path(pipeline_path)
    area_raw = yaml.safe_load(study_area_path.read_text(encoding="utf-8"))
    thresholds = yaml.safe_load(pipeline_path.read_text(encoding="utf-8"))

    area = area_raw["study_area"]
    buildings = tuple(PinnedBuilding.from_yaml(b) for b in area_raw["buildings"])
    if not 8 <= len(buildings) <= 10:
        raise ValueError(f"expected 8-10 pinned buildings, got {len(buildings)}")
    ids = [b.building_id for b in buildings]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate building_id in study area config")
    oids = [b.objectid for b in buildings]
    if len(set(oids)) != len(oids):
        raise ValueError("duplicate objectid in study area config")

    bbox = tuple(float(v) for v in area["bbox_wgs84"])
    if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise ValueError(f"bbox_wgs84 must be (min_lon, min_lat, max_lon, max_lat), got {bbox}")

    # The metric CRS appears in both files: study_area.yaml documents what the area is, and
    # pipeline.yaml is what the code reads. If they ever diverge, geometry silently follows
    # pipeline.yaml and study_area.yaml becomes a lie, so disagreement is a hard error rather
    # than a precedence rule nobody would remember.
    declared = area["crs_metric"]
    operative = thresholds.get("crs", {}).get("metric")
    if operative != declared:
        raise ValueError(
            f"metric CRS disagreement: {study_area_path.name} declares {declared!r} but "
            f"{pipeline_path.name} uses {operative!r}. Published areas would not match the "
            f"documented CRS."
        )

    digest = hashlib.sha256(
        study_area_path.read_bytes() + pipeline_path.read_bytes()
    ).hexdigest()[:16]

    return Config(
        study_area=StudyArea(
            name=area["name"],
            label=area["label"],
            bbox_wgs84=bbox,  # type: ignore[arg-type]
            crs_metric=area["crs_metric"],
            imagery_zoom=int(area["imagery_zoom"]),
            sources=area_raw.get("sources", {}),
            buildings=buildings,
        ),
        thresholds=thresholds,
        config_hash=digest,
        study_area_path=study_area_path,
        pipeline_path=pipeline_path,
    )
