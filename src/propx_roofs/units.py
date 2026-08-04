"""FMZK building parts to building units: buffered fetch, dissolve by ``BW_GEB_ID``.

``ogdwien:FMZKGEBOGD`` polygons are building *parts* — courtyard wings, stair cores,
annexes. A building *unit* is the set of parts sharing a ``BW_GEB_ID``, dissolved
(design section 3.1). Reconnaissance validated the key: Karlsplatz unit 5519487 dissolves
198 parts into one clean building matched by a single roof record at IoU 0.99, so a high
part count is a large real building and not a sentinel identifier.

Two behaviours here exist because of measured failure modes rather than taste:

* **Fetching is buffered before dissolving** (design section 1.4, item 1). 22 of 54 matched
  units in the study area were truncated by the unbuffered bbox, which depresses IoU for
  reasons unrelated to the data.
* **Parts with no ``BW_GEB_ID`` are excluded and counted** (item 6). 105 of 1616 parts in
  the study area carry no identifier. They cannot be dissolved into a unit, and dropping
  them without a number would misrepresent the coverage of the cross-check.

The cached GeoJSON path is the default input, so a normal run is offline and reproducible.
:func:`fetch_parts_live` is separate and explicit, because it is the only function here that
touches the network.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from . import geometry
from .config import Config
from .sources import wfs

logger = logging.getLogger(__name__)

CACHE_FILENAME = "fmzk_parts.geojson"
UNIT_ID_FIELD = "BW_GEB_ID"


@dataclass(frozen=True)
class DissolvedUnit:
    """One ``BW_GEB_ID`` building unit: its parts unioned, still in WGS84.

    Kept in WGS84 because it is an intermediate, not a measurement — projection into
    EPSG:31256 happens in :mod:`propx_roofs.join` via ``geometry.to_metric``, which is the
    only route to a metric number.

    ``touches_fetch_boundary`` is recorded per unit rather than inferred later: a unit that
    reaches the edge of the fetched area may be missing parts that were never downloaded, so
    its IoU is a statement about the query as much as about the join.
    """

    bw_geb_id: int
    geometry: BaseGeometry
    part_count: int
    touches_fetch_boundary: bool


def buffered_fetch_bbox(
    bbox_wgs84: tuple[float, float, float, float], cfg: Config
) -> tuple[float, float, float, float]:
    """The study bbox expanded by ``join.fmzk_buffer_m``, computed in the metric CRS."""
    return geometry.buffer_bbox_m(
        bbox_wgs84,
        float(cfg.threshold("join", "fmzk_buffer_m")),
        cfg.threshold("crs", "metric"),
    )


def default_cache_path(cfg: Config) -> Path:
    """Where the committed FMZK subset for this study area lives."""
    return cfg.study_area.cache_dir / CACHE_FILENAME


def load_parts(cfg: Config, path: Path | str | None = None) -> list[dict[str, Any]]:
    """Read FMZK part features from the cached GeoJSON. Offline; the default input."""
    path = Path(path) if path is not None else default_cache_path(cfg)
    if not path.exists():
        raise FileNotFoundError(
            f"no cached FMZK parts at {path}. Run the fetch step (network) or pass an "
            f"explicit path; the pipeline does not fetch implicitly."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("features", []))


def fetch_parts_live(
    bbox_wgs84: tuple[float, float, float, float],
    cfg: Config,
    *,
    session: Any = None,
    count: int = 20000,
) -> dict[str, Any]:
    """Fetch FMZK parts for ``bbox_wgs84`` expanded by ``join.fmzk_buffer_m``. **Network.**

    Separate from :func:`load_parts` so that no ordinary code path can reach the network by
    accident. Returns the raw FeatureCollection, ready for ``wfs.cache_features``.
    """
    fetch_bbox = buffered_fetch_bbox(bbox_wgs84, cfg)
    logger.info("fetching %s for buffered bbox %s", wfs.LAYER_BUILDING_PARTS, fetch_bbox)
    return wfs.fetch_features(
        wfs.LAYER_BUILDING_PARTS, fetch_bbox, count=count, session=session
    )


def _unit_id(properties: dict[str, Any]) -> int | None:
    """Coerce ``BW_GEB_ID`` to int, or ``None`` if it is absent or not an identifier.

    The city's layers are not consistent about numeric-vs-string typing (design section 1.2
    records ``ONR``/``BEZ`` changing type between editions), so the value is coerced rather
    than compared by type. Anything uncoercible is treated as a missing identifier and
    counted, never guessed at.
    """
    raw = properties.get(UNIT_ID_FIELD)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not an identifier; counted as missing", UNIT_ID_FIELD, raw)
        return None


def dissolve_units(
    features: list[dict[str, Any]],
    fetch_bbox_wgs84: tuple[float, float, float, float],
) -> tuple[dict[int, DissolvedUnit], int]:
    """Dissolve FMZK parts into ``BW_GEB_ID`` units.

    ``fetch_bbox_wgs84`` is the bbox the parts were actually fetched for — the *buffered*
    one, from :func:`buffered_fetch_bbox`. It is required rather than optional because
    ``touches_fetch_boundary`` cannot be honestly filled in without it, and defaulting it to
    the study bbox would flag every unit near the edge of the buffer as clipped.

    Returns ``(units_by_bw_geb_id, n_parts_with_no_identifier)``. The second value is a
    reported figure, not a diagnostic: parts without a ``BW_GEB_ID`` are excluded from the
    cross-check and their count travels with the result (design section 1.4, item 6).
    """
    fetch_area = box(*fetch_bbox_wgs84)
    parts_by_unit: dict[int, list[BaseGeometry]] = defaultdict(list)
    null_id_parts = 0
    no_geometry_parts = 0

    for feature in features:
        raw_geometry = feature.get("geometry")
        if not raw_geometry:
            no_geometry_parts += 1
            continue
        unit_id = _unit_id(feature.get("properties") or {})
        if unit_id is None:
            null_id_parts += 1
            continue
        parts_by_unit[unit_id].append(shape(raw_geometry))

    if no_geometry_parts:
        # Not expected from FMZK; loud rather than absent if it ever happens.
        logger.warning("%d FMZK feature(s) had no geometry and were skipped", no_geometry_parts)
    logger.info(
        "dissolved %d FMZK parts into %d units; %d part(s) had no %s",
        sum(len(v) for v in parts_by_unit.values()),
        len(parts_by_unit),
        null_id_parts,
        UNIT_ID_FIELD,
    )

    units: dict[int, DissolvedUnit] = {}
    for unit_id, geoms in parts_by_unit.items():
        dissolved = unary_union(geoms)
        units[unit_id] = DissolvedUnit(
            bw_geb_id=unit_id,
            geometry=dissolved,
            part_count=len(geoms),
            # Containment is topological, so this needs no metric frame.
            touches_fetch_boundary=not fetch_area.contains(dissolved),
        )
    return units, null_id_parts


def load_units(
    cfg: Config, path: Path | str | None = None
) -> tuple[dict[int, DissolvedUnit], int, tuple[float, float, float, float]]:
    """Cached-path convenience wrapper: load, dissolve, and report the fetch bbox used.

    **Assumption, and it is checkable only weakly:** the cached file was fetched for the
    *buffered* bbox. A GeoJSON FeatureCollection carries no record of the query that produced
    it, so if the cache was fetched for a smaller box then every unit will be reported as not
    touching the fetch boundary while some were in fact truncated. :func:`_warn_if_cache_is_smaller`
    catches the obvious version of that mistake and warns; it cannot prove the converse.

    The fetch bbox is returned rather than kept private so that a caller publishing
    ``fmzk_fetch_buffer_m`` is publishing the box the units were actually judged against.
    """
    fetch_bbox = buffered_fetch_bbox(cfg.study_area.bbox_wgs84, cfg)
    features = load_parts(cfg, path)
    _warn_if_cache_is_smaller(features, cfg, fetch_bbox)
    units, null_id_parts = dissolve_units(features, fetch_bbox)
    return units, null_id_parts, fetch_bbox


def _warn_if_cache_is_smaller(
    features: list[dict[str, Any]],
    cfg: Config,
    fetch_bbox: tuple[float, float, float, float],
) -> None:
    """Warn when cached parts plainly do not reach the buffered fetch bbox.

    Half the configured buffer is the test: real data can legitimately stop short of the edge
    in a sparse area, but stopping short by more than half the buffer on *every* side means the
    cache was almost certainly fetched for a smaller box, and ``touches_fetch_boundary`` would
    then understate truncation for the whole study area.
    """
    extents = [shape(f["geometry"]).bounds for f in features if f.get("geometry")]
    if not extents:
        return
    observed = (
        min(e[0] for e in extents),
        min(e[1] for e in extents),
        max(e[2] for e in extents),
        max(e[3] for e in extents),
    )
    buffer_m = float(cfg.threshold("join", "fmzk_buffer_m"))
    half_buffered = geometry.buffer_bbox_m(
        cfg.study_area.bbox_wgs84, buffer_m / 2.0, cfg.threshold("crs", "metric")
    )
    if box(*half_buffered).contains(box(*observed)):
        logger.warning(
            "cached FMZK extent %s falls well inside the buffered fetch bbox %s. The cache was "
            "probably fetched without the %g m buffer, so unit_touches_fetch_boundary will "
            "understate truncation. Re-run the fetch step via fetch_parts_live().",
            tuple(round(v, 6) for v in observed),
            tuple(round(v, 6) for v in fetch_bbox),
            buffer_m,
        )
