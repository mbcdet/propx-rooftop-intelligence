"""Tile maths and mosaic georeferencing.

These run fully offline. They are the part of the imagery path that can be wrong silently,
so they are tested independently of any network access.
"""

from __future__ import annotations

import math

from propx_roofs.sources import wmts

VIENNA_LAT = 48.2


def test_resolution_matches_published_scale_denominators() -> None:
    # The lb2024 TileMatrixSet google3857_0-21 publishes these scale denominators.
    # At 0.28 mm nominal pixel size, resolution = scale_denominator * 0.00028.
    for zoom, scale_denominator in ((19, 1066.36479193), (20, 533.18239597), (21, 266.59119798)):
        assert math.isclose(wmts.resolution(zoom), scale_denominator * 0.00028, rel_tol=1e-6)


def test_effective_resolution_at_vienna() -> None:
    """z20 is the smallest zoom that does not undersample the 15 cm source."""
    assert math.isclose(wmts.resolution_at_latitude(19, VIENNA_LAT), 0.1991, abs_tol=5e-4)
    assert math.isclose(wmts.resolution_at_latitude(20, VIENNA_LAT), 0.0995, abs_tol=5e-4)
    assert wmts.resolution_at_latitude(19, VIENNA_LAT) > 0.15  # undersamples
    assert wmts.resolution_at_latitude(20, VIENNA_LAT) < 0.15  # does not


def test_lonlat_meters_roundtrip() -> None:
    for lon, lat in ((16.3722, 48.2082), (16.5060, 48.2255), (16.3790, 48.1855)):
        x, y = wmts.lonlat_to_meters(lon, lat)
        back_lon, back_lat = wmts.meters_to_lonlat(x, y)
        assert math.isclose(back_lon, lon, abs_tol=1e-9)
        assert math.isclose(back_lat, lat, abs_tol=1e-9)


def test_tile_range_is_inclusive_and_ordered() -> None:
    bbox = (16.3660, 48.1960, 16.3720, 48.2010)
    col_min, row_min, col_max, row_max = wmts.tile_range(bbox, 20)
    assert col_min <= col_max and row_min <= row_max
    # Row indices increase southwards, so the northern edge yields the smaller row.
    _, north_row = wmts.meters_to_tile(*wmts.lonlat_to_meters(bbox[0], bbox[3]), 20)
    _, south_row = wmts.meters_to_tile(*wmts.lonlat_to_meters(bbox[0], bbox[1]), 20)
    assert north_row <= south_row


def test_transform_maps_bbox_corners_inside_the_mosaic() -> None:
    """The assembled mosaic must contain the requested bbox, with correct orientation."""
    bbox = (16.3660, 48.1960, 16.3720, 48.2010)
    zoom = 20
    col_min, row_min, col_max, row_max = wmts.tile_range(bbox, zoom)
    span = wmts.resolution(zoom) * wmts.TILE_SIZE
    transform = wmts.MosaicTransform(
        crs="EPSG:3857",
        zoom=zoom,
        origin_x=col_min * span - wmts.ORIGIN_SHIFT,
        origin_y=wmts.ORIGIN_SHIFT - row_min * span,
        pixel_size=wmts.resolution(zoom),
        width=(col_max - col_min + 1) * wmts.TILE_SIZE,
        height=(row_max - row_min + 1) * wmts.TILE_SIZE,
        tile_col_min=col_min,
        tile_row_min=row_min,
        layer="lb2024",
    )

    upper_left = transform.pixel_of(bbox[0], bbox[3])
    lower_right = transform.pixel_of(bbox[2], bbox[1])

    for px, py in (upper_left, lower_right):
        assert 0 <= px <= transform.width
        assert 0 <= py <= transform.height
    # x increases eastwards, y increases southwards.
    assert upper_left[0] < lower_right[0]
    assert upper_left[1] < lower_right[1]


def test_pixel_extent_matches_ground_extent() -> None:
    """Pixel distance across the bbox must equal ground distance / pixel size."""
    bbox = (16.3660, 48.1960, 16.3720, 48.2010)
    zoom = 20
    col_min, row_min, col_max, row_max = wmts.tile_range(bbox, zoom)
    span = wmts.resolution(zoom) * wmts.TILE_SIZE
    transform = wmts.MosaicTransform(
        crs="EPSG:3857",
        zoom=zoom,
        origin_x=col_min * span - wmts.ORIGIN_SHIFT,
        origin_y=wmts.ORIGIN_SHIFT - row_min * span,
        pixel_size=wmts.resolution(zoom),
        width=(col_max - col_min + 1) * wmts.TILE_SIZE,
        height=(row_max - row_min + 1) * wmts.TILE_SIZE,
        tile_col_min=col_min,
        tile_row_min=row_min,
        layer="lb2024",
    )
    x0, _ = wmts.lonlat_to_meters(bbox[0], bbox[3])
    x1, _ = wmts.lonlat_to_meters(bbox[2], bbox[3])
    expected_px = (x1 - x0) / transform.pixel_size
    actual_px = transform.pixel_of(bbox[2], bbox[3])[0] - transform.pixel_of(bbox[0], bbox[3])[0]
    assert math.isclose(actual_px, expected_px, rel_tol=1e-9)
