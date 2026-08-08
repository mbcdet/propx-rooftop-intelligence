"""Vienna OGD WFS access.

Two hard-won conventions are enforced here rather than left to callers:

1. **WFS 2.0.0 with an explicit CRS URI on the bbox.** Verified 2026-08-03: with
   ``version=1.1.0`` and a bare ``bbox``, the service returns HTTP 200 with an *empty*
   FeatureCollection in **both** coordinate orders. An empty 200 reads like "nothing here"
   rather than a malformed request, so it is the most dangerous possible failure mode.
   With ``version=2.0.0`` and ``urn:ogc:def:crs:EPSG::4326`` appended, the same bbox
   returns the expected features.
2. **lat,lon axis order** for that URN form. (The short ``EPSG:4326`` form uses lon,lat —
   also verified. We do not use the short form.)

``fetch_features`` pages with WFS 2.0.0 ``startIndex``/``count`` until the service has handed
over everything it matched, deduplicates by feature id (pages of an unstably sorted result can
overlap), and **raises** :class:`TruncatedResultError` when the unique features received do not
reconcile with ``numberMatched`` — a truncated fetch must never be cached as if it were the
whole extent (RTI-018).

Licence for all layers: CC BY 4.0. Attribution: "Datenquelle: Stadt Wien - data.wien.gv.at".
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

WFS_ENDPOINT: Final[str] = "https://data.wien.gv.at/daten/geo"
CRS_URN: Final[str] = "urn:ogc:def:crs:EPSG::4326"
ATTRIBUTION: Final[str] = "Datenquelle: Stadt Wien - data.wien.gv.at"
USER_AGENT: Final[str] = "propx-rooftop-intelligence/0.1 (technical assessment)"

# Baseline layers, verified live on 2026-08-03 with the feature counts shown.
LAYER_ROOF_RECORD_2025: Final[str] = "ogdwien:ANLAGENLEISTUNG2025OGD"  # 191_625
LAYER_ROOF_RECORD_2022: Final[str] = "ogdwien:ANLAGENLEISTUNGOGD"  # 185_261, comparison only
LAYER_BUILDING_PARTS: Final[str] = "ogdwien:FMZKGEBOGD"  # 780_618
LAYER_TYPOLOGY: Final[str] = "ogdwien:GEBAEUDETYPOGD"  # 438_840
LAYER_BUILDING_INFO: Final[str] = "ogdwien:GEBAEUDEINFOOGD"  # 58_255
LAYER_PV_SUBAREAS_2025: Final[str] = "ogdwien:PVPOTENZIALE2025OGD"  # 1_700_559, optional


class EmptyResultError(RuntimeError):
    """Raised when a query the caller expected to be populated returned nothing.

    Exists so that the silent-empty-200 failure mode becomes loud.
    """


class TruncatedResultError(RuntimeError):
    """Raised when paging finished but completeness could not be established.

    Before RTI-018 a truncated response was a log warning and the partial data flowed on into
    the cache; a cache that silently holds a subset of the extent it claims is exactly the
    kind of wrongness that never fails at runtime, so now it is an exception.
    """


def _bbox_param(bbox_wgs84: tuple[float, float, float, float]) -> str:
    """Format (min_lon, min_lat, max_lon, max_lat) for WFS 2.0.0 as lat,lon + CRS URN."""
    min_lon, min_lat, max_lon, max_lat = bbox_wgs84
    return f"{min_lat},{min_lon},{max_lat},{max_lon},{CRS_URN}"


def _get_page(
    http: Any, layer: str, params: dict[str, str], timeout: float
) -> dict[str, Any]:
    """One GetFeature request, with the XML-ServiceException-as-HTTP-200 trap made loud."""
    response = http.get(
        WFS_ENDPOINT, params=params, timeout=timeout, headers={"User-Agent": USER_AGENT}
    )
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "")
    if "json" not in content_type:
        # The service reports request errors as an XML ServiceException with HTTP 200.
        raise RuntimeError(
            f"{layer}: expected JSON but got {content_type!r}. "
            f"First 300 bytes: {response.text[:300]!r}"
        )
    return response.json()


def fetch_features(
    layer: str,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    property_names: Sequence[str] | None = None,
    count: int = 5000,
    timeout: float = 120.0,
    require_non_empty: bool = True,
    session=None,
    max_pages: int = 500,
) -> dict[str, Any]:
    """Fetch the **complete** GeoJSON FeatureCollection for ``layer`` within ``bbox_wgs84``.

    Pages with WFS 2.0.0 ``startIndex``/``count`` (RTI-018): ``count`` is the page size, not a
    cap on the result. Features are deduplicated by their GeoJSON ``id`` — pages of an
    unstably sorted result can overlap — and after paging the unique total is reconciled
    against ``numberMatched``. A shortfall (or surplus) raises :class:`TruncatedResultError`,
    because completeness could not be established and cached partial data lies about its
    extent. When the service does not state ``numberMatched`` (the spec allows "unknown"),
    paging stops at the first short page and the result is taken as complete.

    Set ``property_names`` to omit geometry and return attributes only — useful for cheap
    reconnaissance. Set ``require_non_empty=False`` for queries that may legitimately
    match nothing.
    """
    import requests

    base_params: dict[str, str] = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": layer,
        "srsName": CRS_URN,
        "outputFormat": "json",
        "bbox": _bbox_param(bbox_wgs84),
    }
    if property_names:
        base_params["propertyName"] = ",".join(property_names)

    http = session or requests.Session()

    envelope: dict[str, Any] = {}
    features: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    duplicates = 0
    matched: int | None = None
    cursor = 0  # raw features received, before deduplication: the startIndex to page from

    for page_number in range(max_pages):
        params = dict(base_params, count=str(count), startIndex=str(cursor))
        page = _get_page(http, layer, params, timeout)
        page_features = page.get("features", [])
        if page_number == 0:
            envelope = {key: value for key, value in page.items() if key != "features"}
            reported = page.get("numberMatched")
            matched = reported if isinstance(reported, int) else None
        logger.info(
            "%s: page %d startIndex=%d returned=%d numberMatched=%s",
            layer,
            page_number,
            cursor,
            len(page_features),
            matched if matched is not None else "unknown",
        )
        for feature in page_features:
            feature_id = feature.get("id")
            if feature_id is not None:
                if feature_id in seen_ids:
                    duplicates += 1
                    continue
                seen_ids.add(feature_id)
            features.append(feature)
        cursor += len(page_features)
        if matched is not None and cursor >= matched:
            break
        if len(page_features) < count:
            break  # the service has nothing further at this startIndex
    else:
        raise TruncatedResultError(
            f"{layer}: still incomplete after {max_pages} pages of {count} "
            f"({len(features)} unique features, numberMatched={matched}); refusing to treat "
            f"the partial result as the full extent"
        )

    if duplicates:
        logger.info("%s: dropped %d duplicate feature(s) across pages", layer, duplicates)
    if matched is not None and len(features) != matched:
        raise TruncatedResultError(
            f"{layer}: paged fetch yielded {len(features)} unique features "
            f"({duplicates} duplicate(s) dropped, {cursor} received) but the service "
            f"reported numberMatched={matched}; completeness cannot be established, so the "
            f"result is refused rather than cached as if it were the whole extent"
        )
    if require_non_empty and not features:
        raise EmptyResultError(
            f"{layer} returned 0 features for bbox {bbox_wgs84}. This is usually a bbox "
            f"axis-order or CRS problem rather than genuinely empty ground - the service "
            f"answers malformed spatial queries with an empty HTTP 200."
        )

    payload = dict(envelope)
    payload["features"] = features
    payload["numberReturned"] = len(features)
    return payload


def fetch_count(
    layer: str,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    timeout: float = 60.0,
    session=None,
) -> int:
    """Return ``numberMatched`` for ``layer`` in ``bbox_wgs84`` without transferring features."""
    import re

    import requests

    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": layer,
        "srsName": CRS_URN,
        "resultType": "hits",
        "bbox": _bbox_param(bbox_wgs84),
    }
    http = session or requests.Session()
    response = http.get(
        WFS_ENDPOINT, params=params, timeout=timeout, headers={"User-Agent": USER_AGENT}
    )
    response.raise_for_status()
    match = re.search(r'numberMatched="(\d+)"', response.text)
    if not match:
        raise RuntimeError(f"{layer}: no numberMatched in hits response: {response.text[:300]!r}")
    return int(match.group(1))


def cache_features(payload: dict[str, Any], path: Path) -> Path:
    """Write a FeatureCollection to ``path`` with stable key ordering."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8"
    )
    return path
