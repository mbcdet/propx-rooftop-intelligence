"""Geometry: the projected frame is the only source of a published number.

These tests run fully offline on polygons built in code. Nothing here reads ``data/cache``,
because the properties under test are properties of the transform, not of Vienna.

The reference square is built with ``pyproj.Geod``, which measures on the WGS84 ellipsoid
independently of any projected CRS. That way the 900 m2 assertion checks EPSG:31256 against
an outside authority rather than against another part of the same code path.
"""

from __future__ import annotations

import math

import pytest
from pyproj import Geod
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry

from propx_roofs import geometry
from propx_roofs.types import MetricGeometry

GEOD = Geod(ellps="WGS84")

# Inside the pinned Sonnwendviertel study area, so the numbers are representative of the
# latitude and the distance from the EPSG:31256 central meridian that the pipeline works at.
STUDY_BBOX = (16.3760, 48.1830, 16.3820, 48.1880)
LON0, LAT0 = 16.3790, 48.1855


def _geod_square(side_m: float, lon: float = LON0, lat: float = LAT0) -> Polygon:
    """A square of ``side_m`` built from geodesic offsets, independent of any projected CRS."""
    east_lon, east_lat, _ = GEOD.fwd(lon, lat, 90.0, side_m)
    north_lon, north_lat, _ = GEOD.fwd(lon, lat, 0.0, side_m)
    far_lon, far_lat, _ = GEOD.fwd(north_lon, north_lat, 90.0, side_m)
    return Polygon([(lon, lat), (east_lon, east_lat), (far_lon, far_lat), (north_lon, north_lat)])


@pytest.mark.parametrize(
    "geographic_crs",
    [
        "EPSG:4326",  # WGS84 lat/lon, the CRS every source geometry arrives in
        "EPSG:4258",  # ETRS89 geographic
        "EPSG:4979",  # WGS84 geographic 3D
    ],
)
def test_to_metric_refuses_a_geographic_crs(geographic_crs: str) -> None:
    """Square degrees look like a plausible small area, so the refusal must be at the door."""
    with pytest.raises(ValueError, match="cannot produce a metric value"):
        geometry.to_metric(_geod_square(30.0), geographic_crs)


def test_projected_crs_accepts_the_pipeline_crs() -> None:
    assert geometry.projected_crs(geometry.DEFAULT_METRIC_CRS).is_projected


@pytest.mark.parametrize("polygon", [_geod_square(30.0), box(*STUDY_BBOX)])
def test_projected_and_diagnostic_areas_are_not_interchangeable(polygon: Polygon) -> None:
    """Amendment B3: the cos(lat) recon frame may never produce a published area.

    The two frames must disagree by more than floating-point noise, otherwise a future
    refactor could quietly swap one for the other and nothing would notice.
    """
    _, metric = geometry.to_metric(polygon)
    diagnostic = geometry.local_diagnostic_scale(polygon, LAT0)

    relative_difference = abs(diagnostic.area - metric.area_m2) / metric.area_m2
    assert relative_difference > 1e-3, "frames agree too closely to stay distinguishable"
    assert not math.isclose(diagnostic.area, metric.area_m2, rel_tol=1e-3)

    # The diagnostic frame cannot even express a published value: it hands back a bare
    # geometry, and every contract that publishes an area demands a MetricGeometry.
    assert isinstance(diagnostic, BaseGeometry)
    assert not isinstance(diagnostic, MetricGeometry)
    assert isinstance(metric, MetricGeometry)


def test_known_square_area_matches_the_ellipsoidal_value() -> None:
    """A 30 m geodesic square is ~900 m2 in EPSG:31256, within 1%."""
    square = _geod_square(30.0)
    _, metric = geometry.to_metric(square)

    assert metric.area_m2 == pytest.approx(900.0, rel=0.01)
    ellipsoidal_area = abs(GEOD.geometry_area_perimeter(square)[0])
    assert metric.area_m2 == pytest.approx(ellipsoidal_area, rel=0.01)
    assert metric.crs == "EPSG:31256"
    assert (metric.n_polygons, metric.n_interior_rings) == (1, 0)


def test_courtyard_void_is_excluded_from_area_and_reported_separately() -> None:
    """A courtyard is not roof. vie-swv-001 has a 238 m2 void; counting it would inflate PV."""
    outer = _geod_square(40.0)
    inner = _geod_square(10.0, *GEOD.fwd(LON0, LAT0, 45.0, 20.0)[:2])
    with_courtyard = Polygon(outer.exterior.coords, [inner.exterior.coords])

    _, solid = geometry.to_metric(outer)
    _, holed = geometry.to_metric(with_courtyard)

    assert holed.n_interior_rings == 1
    assert holed.interior_ring_area_m2 == pytest.approx(100.0, rel=0.01)
    # area_m2 excludes the void, and the void area is published rather than merely dropped.
    assert holed.area_m2 == pytest.approx(solid.area_m2 - holed.interior_ring_area_m2, rel=1e-9)
    assert holed.area_m2 < solid.area_m2


def test_buffered_bbox_grows_and_zero_buffer_is_near_identity() -> None:
    buffered = geometry.buffer_bbox_m(STUDY_BBOX, 150.0)
    min_lon, min_lat, max_lon, max_lat = buffered

    assert min_lon < STUDY_BBOX[0] and min_lat < STUDY_BBOX[1]
    assert max_lon > STUDY_BBOX[2] and max_lat > STUDY_BBOX[3]
    assert box(*buffered).contains(box(*STUDY_BBOX))

    # 150 m of latitude is ~0.00135 deg; the east-west buffer must be wider in degrees at
    # 48 deg N, which is the whole reason the buffer is applied in the projected CRS.
    assert (STUDY_BBOX[1] - min_lat) == pytest.approx(0.00135, abs=2e-4)
    assert (STUDY_BBOX[0] - min_lon) > (STUDY_BBOX[1] - min_lat)

    # A WGS84-aligned rectangle is slightly rotated in EPSG:31256, so a zero buffer returns a
    # marginally larger box rather than the identity. It errs outwards, by well under a metre.
    unbuffered = geometry.buffer_bbox_m(STUDY_BBOX, 0.0)
    assert box(*unbuffered).contains(box(*STUDY_BBOX))
    assert unbuffered == pytest.approx(STUDY_BBOX, abs=1e-5)


def test_negative_buffer_is_refused() -> None:
    with pytest.raises(ValueError, match="must be >= 0"):
        geometry.buffer_bbox_m(STUDY_BBOX, -10.0)


def test_azimuth_is_undirected_so_north_and_south_agree() -> None:
    """A ridge has no direction: reporting 0 for one end and 180 for the other is a bug."""
    south, north = (LON0, 48.1850), (LON0, 48.1860)

    northward = geometry.azimuth_deg(south, north)
    southward = geometry.azimuth_deg(north, south)

    assert northward == pytest.approx(southward)
    assert 0.0 <= northward < 180.0
    # Grid north, not true north: EPSG:31256's central meridian is 16 deg 20', so a
    # geographically north-south line is off grid north by the meridian convergence here
    # (~0.03 deg) and folds to just under 180 rather than to exactly 0.
    assert min(northward, 180.0 - northward) < 0.1

    eastward = geometry.azimuth_deg((16.3780, LAT0), (16.3800, LAT0))
    assert eastward == pytest.approx(90.0, abs=0.1)


def test_min_rotated_rect_axis_finds_the_long_side() -> None:
    """footprint_axis_orientation_deg is a building axis, so the long side must win."""
    metric, _ = geometry.to_metric(box(*STUDY_BBOX))
    azimuth, long_m, short_m = geometry.min_rotated_rect_axis(metric)

    assert long_m > short_m
    # The study bbox is ~450 m wide and ~555 m tall, so the long axis runs roughly north-south.
    assert long_m == pytest.approx(555.0, rel=0.05)
    assert short_m == pytest.approx(447.0, rel=0.05)
    assert min(azimuth, 180.0 - azimuth) < 1.0


def test_degenerate_geometry_has_no_axis() -> None:
    """A zero-area outline yields no rectangle; refuse rather than fabricate an axis.

    Built directly in projected metres, because a constant-latitude line in WGS84 is very
    slightly curved in EPSG:31256 and therefore is *not* degenerate once projected.
    """
    collinear = Polygon([(3480.0, 338500.0), (3490.0, 338500.0), (3500.0, 338500.0)])
    with pytest.raises(ValueError, match="degenerate"):
        geometry.min_rotated_rect_axis(collinear)
