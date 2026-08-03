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


def _bbox_param(bbox_wgs84: tuple[float, float, float, float]) -> str:
    """Format (min_lon, min_lat, max_lon, max_lat) for WFS 2.0.0 as lat,lon + CRS URN."""
    min_lon, min_lat, max_lon, max_lat = bbox_wgs84
    return f"{min_lat},{min_lon},{max_lat},{max_lon},{CRS_URN}"


def fetch_features(
    layer: str,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    property_names: Sequence[str] | None = None,
    count: int = 5000,
    timeout: float = 120.0,
    require_non_empty: bool = True,
    session=None,
) -> dict[str, Any]:
    """Fetch a GeoJSON FeatureCollection for ``layer`` within ``bbox_wgs84``.

    Set ``property_names`` to omit geometry and return attributes only — useful for cheap
    reconnaissance. Set ``require_non_empty=False`` for queries that may legitimately
    match nothing.
    """
    import requests

    params: dict[str, str] = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": layer,
        "srsName": CRS_URN,
        "outputFormat": "json",
        "count": str(count),
        "bbox": _bbox_param(bbox_wgs84),
    }
    if property_names:
        params["propertyName"] = ",".join(property_names)

    http = session or requests.Session()
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

    payload = response.json()
    matched = payload.get("numberMatched", 0)
    returned = len(payload.get("features", []))
    logger.info("%s: numberMatched=%s numberReturned=%d", layer, matched, returned)

    if require_non_empty and returned == 0:
        raise EmptyResultError(
            f"{layer} returned 0 features for bbox {bbox_wgs84}. This is usually a bbox "
            f"axis-order or CRS problem rather than genuinely empty ground - the service "
            f"answers malformed spatial queries with an empty HTTP 200."
        )
    if returned and matched and returned < matched:
        logger.warning(
            "%s: truncated at count=%d (%s matched); raise count or tile the query",
            layer,
            count,
            matched,
        )
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
