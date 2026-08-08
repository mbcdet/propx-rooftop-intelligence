"""WFS paging (RTI-018). Fully offline: every response below is a scripted mock.

Before RTI-018, ``fetch_features`` sent a single request and merely *warned* when
``numberReturned < numberMatched`` — truncated data flowed on into the cache looking complete.
Now the fetch pages with ``startIndex``/``count``, deduplicates by feature id (pages of an
unstably sorted result can overlap), reconciles the unique total against ``numberMatched``,
and raises :class:`~propx_roofs.sources.wfs.TruncatedResultError` when completeness cannot be
established. These tests pin all of that, plus the hard-won axis-order conventions from the
module docstring, which the paging rework was not allowed to disturb.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from propx_roofs.sources import wfs

BBOX = (16.376, 48.183, 16.382, 48.188)


def _feature(n: int) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": f"TESTLAYER.{n}",
        "geometry": {"type": "Point", "coordinates": [16.379, 48.185]},
        "properties": {"OBJECTID": n},
    }


def _page(features: list[dict[str, Any]], matched: int | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": features,
        "numberReturned": len(features),
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
    }
    if matched is not None:
        payload["numberMatched"] = matched
    return payload


class _Response:
    def __init__(self, payload: Any, content_type: str = "application/json") -> None:
        self._payload = payload
        self.headers = {"Content-Type": content_type}
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _Session:
    """Serves scripted pages in order and records every request's params."""

    def __init__(self, pages: list[Any]) -> None:
        self.pages = list(pages)
        self.requests: list[dict[str, str]] = []

    def get(self, url: str, params: dict[str, str], timeout: float, headers: dict) -> _Response:
        self.requests.append(dict(params))
        if not self.pages:
            raise AssertionError("fetch_features requested more pages than were scripted")
        page = self.pages.pop(0)
        return page if isinstance(page, _Response) else _Response(page)


# --- paging -----------------------------------------------------------------------------------


def test_a_multi_page_result_is_fetched_completely_in_order() -> None:
    session = _Session(
        [
            _page([_feature(1), _feature(2)], matched=5),
            _page([_feature(3), _feature(4)], matched=5),
            _page([_feature(5)], matched=5),
        ]
    )
    payload = wfs.fetch_features("test:layer", BBOX, count=2, session=session)

    assert [f["id"] for f in payload["features"]] == [f"TESTLAYER.{n}" for n in range(1, 6)]
    assert payload["numberReturned"] == 5
    assert payload["numberMatched"] == 5
    assert [request["startIndex"] for request in session.requests] == ["0", "2", "4"]
    assert all(request["count"] == "2" for request in session.requests)


def test_a_single_complete_page_needs_exactly_one_request() -> None:
    session = _Session([_page([_feature(1), _feature(2)], matched=2)])
    payload = wfs.fetch_features("test:layer", BBOX, count=5000, session=session)

    assert len(session.requests) == 1
    assert payload["numberReturned"] == 2


def test_duplicate_features_across_pages_are_dropped_and_the_result_still_reconciles() -> None:
    """Pages of an unstably sorted result can overlap; the union must still be complete."""
    session = _Session(
        [
            _page([_feature(1), _feature(2)], matched=3),
            _page([_feature(2), _feature(3)], matched=3),  # feature 2 repeated across pages
        ]
    )
    payload = wfs.fetch_features("test:layer", BBOX, count=2, session=session)

    ids = [f["id"] for f in payload["features"]]
    assert ids == ["TESTLAYER.1", "TESTLAYER.2", "TESTLAYER.3"]
    assert payload["numberReturned"] == 3


def test_an_under_returning_service_raises_instead_of_caching_a_subset() -> None:
    """The service claims 10 matches but hands over 3 and stops: completeness unprovable."""
    session = _Session([_page([_feature(n) for n in (1, 2, 3)], matched=10)])
    with pytest.raises(wfs.TruncatedResultError, match="numberMatched=10"):
        wfs.fetch_features("test:layer", BBOX, count=5, session=session)


def test_an_empty_continuation_page_with_matches_outstanding_raises() -> None:
    session = _Session(
        [
            _page([_feature(1), _feature(2)], matched=4),
            _page([], matched=4),
        ]
    )
    with pytest.raises(wfs.TruncatedResultError, match="completeness cannot be established"):
        wfs.fetch_features("test:layer", BBOX, count=2, session=session)


def test_duplicates_that_hide_a_missing_feature_raise() -> None:
    """Raw count reaches numberMatched but the unique union does not: refuse, do not guess."""
    session = _Session(
        [
            _page([_feature(1), _feature(2)], matched=4),
            _page([_feature(2), _feature(1)], matched=4),  # nothing new; two features missing
        ]
    )
    with pytest.raises(wfs.TruncatedResultError, match="duplicate"):
        wfs.fetch_features("test:layer", BBOX, count=2, session=session)


def test_unknown_number_matched_pages_until_a_short_page() -> None:
    """The WFS spec allows numberMatched to be unstated; a short page then ends the fetch."""
    session = _Session(
        [
            _page([_feature(1), _feature(2)], matched=None),
            _page([_feature(3)], matched=None),
        ]
    )
    payload = wfs.fetch_features("test:layer", BBOX, count=2, session=session)
    assert [f["id"] for f in payload["features"]] == [
        "TESTLAYER.1",
        "TESTLAYER.2",
        "TESTLAYER.3",
    ]


# --- the pre-existing failure modes stay loud --------------------------------------------------


def test_an_empty_result_raises_when_the_caller_expected_features() -> None:
    session = _Session([_page([], matched=0)])
    with pytest.raises(wfs.EmptyResultError, match="axis-order or CRS problem"):
        wfs.fetch_features("test:layer", BBOX, session=session)


def test_an_empty_result_is_allowed_when_declared_legitimate() -> None:
    session = _Session([_page([], matched=0)])
    payload = wfs.fetch_features("test:layer", BBOX, require_non_empty=False, session=session)
    assert payload["features"] == []
    assert payload["numberReturned"] == 0


def test_an_xml_service_exception_with_http_200_is_an_error() -> None:
    session = _Session(
        [_Response("<ServiceExceptionReport>bad request</ServiceExceptionReport>", "text/xml")]
    )
    with pytest.raises(RuntimeError, match="expected JSON"):
        wfs.fetch_features("test:layer", BBOX, session=session)


# --- the hard-won request conventions are untouched by the paging rework ----------------------


def test_every_page_keeps_the_wfs2_urn_and_lat_lon_axis_order() -> None:
    """Module docstring conventions: version 2.0.0, CRS URN, and lat,lon order for that URN."""
    session = _Session(
        [
            _page([_feature(1), _feature(2)], matched=3),
            _page([_feature(3)], matched=3),
        ]
    )
    wfs.fetch_features("test:layer", BBOX, count=2, session=session)

    for request in session.requests:
        assert request["version"] == "2.0.0"
        assert request["srsName"] == wfs.CRS_URN
        assert request["bbox"] == f"48.183,16.376,48.188,16.382,{wfs.CRS_URN}"
