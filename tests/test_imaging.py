"""Crop assembly, mask rasterisation and shadow statistics.

Fully offline. Unit tests build their imagery in code so they assert behaviour rather than the
contents of a cache, and the one cache-backed test is skipped when no usable tiles are committed.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from shapely.geometry import Polygon

from propx_roofs import config as config_module
from propx_roofs import imaging
from propx_roofs.sources import wmts
from propx_roofs.types import ImageCrop

CFG = config_module.load()

# Inside the pinned study area, so a synthetic crop sits at the latitude the pipeline runs at and
# the Web-Mercator-to-ground scale factor is the real one.
CENTRE_LON, CENTRE_LAT = 16.3790, 48.1865


def override(cfg: Any, path: tuple[str, ...], value: Any) -> Any:
    """A config clone with one threshold changed, for testing threshold behaviour itself."""
    thresholds = copy.deepcopy(getattr(cfg, "thresholds", cfg))
    node = thresholds
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    return type("OverriddenConfig", (), {"thresholds": thresholds})()


def square_wgs84(half_size_m: float = 15.0, hole_m: float | None = None) -> Polygon:
    """A square roof outline centred in the study area, optionally with a courtyard void."""
    import math

    d_lat = half_size_m / 110574.0
    d_lon = half_size_m / (111320.0 * math.cos(math.radians(CENTRE_LAT)))
    shell = [
        (CENTRE_LON - d_lon, CENTRE_LAT - d_lat),
        (CENTRE_LON + d_lon, CENTRE_LAT - d_lat),
        (CENTRE_LON + d_lon, CENTRE_LAT + d_lat),
        (CENTRE_LON - d_lon, CENTRE_LAT + d_lat),
    ]
    if hole_m is None:
        return Polygon(shell)
    h_lat = hole_m / 110574.0
    h_lon = hole_m / (111320.0 * math.cos(math.radians(CENTRE_LAT)))
    hole = [
        (CENTRE_LON - h_lon, CENTRE_LAT - h_lat),
        (CENTRE_LON + h_lon, CENTRE_LAT - h_lat),
        (CENTRE_LON + h_lon, CENTRE_LAT + h_lat),
        (CENTRE_LON - h_lon, CENTRE_LAT + h_lat),
    ]
    return Polygon(shell, [hole])


def synthetic_crop(
    rgb: np.ndarray,
    mask: np.ndarray | None = None,
    missing_tiles: tuple[str, ...] = (),
    zoom: int = 20,
) -> ImageCrop:
    """An ``ImageCrop`` georeferenced in the study area, built from arrays rather than tiles."""
    if mask is None:
        mask = np.ones(rgb.shape[:2], dtype=bool)
    origin_x, origin_y = wmts.lonlat_to_meters(CENTRE_LON, CENTRE_LAT)
    return ImageCrop(
        rgb=np.ascontiguousarray(rgb.astype(np.uint8)),
        roof_mask=np.asarray(mask, dtype=bool),
        origin_x=origin_x,
        origin_y=origin_y,
        pixel_size_m=wmts.resolution(zoom),
        zoom=zoom,
        missing_tiles=missing_tiles,
    )


def write_tile(root: Path, tile_id: str, value: int = 190) -> None:
    """Write a uniform JPEG tile at the cache path a missing-tile identifier names."""
    layer, zoom, row, col = tile_id.split("/")
    path = root / layer / zoom / row / f"{col}.jpeg"
    path.parent.mkdir(parents=True, exist_ok=True)
    tile = np.full((wmts.TILE_SIZE, wmts.TILE_SIZE, 3), value, dtype=np.uint8)
    cv2.imwrite(str(path), tile)


def usable_tile_cache() -> Path | None:
    """The committed cache directory, or ``None`` if it holds no readable tile.

    Guards the one integration test. The cache is checked for *content*, not existence: the
    repository can carry the directory structure with empty placeholder files, and a test that
    passed on those would be asserting nothing.
    """
    area = CFG.study_area
    root = area.cache_dir / "tiles" / str(CFG.threshold("imagery", "layer"))
    if not root.is_dir():
        return None
    for path in root.rglob("*.jpeg"):
        if path.stat().st_size > 0:
            return area.cache_dir
    return None


# --------------------------------------------------------------------------------------------
# threshold access


def test_missing_threshold_raises_rather_than_defaulting() -> None:
    with pytest.raises(KeyError):
        imaging.threshold(CFG, "image", "no_such_threshold")
    assert imaging.threshold(CFG, "image", "no_such_threshold", default=7.0) == 7.0


# --------------------------------------------------------------------------------------------
# georeferencing


def test_ground_pixel_size_is_the_design_value_not_the_mercator_one() -> None:
    crop = synthetic_crop(np.zeros((64, 64, 3), np.uint8))
    # Design section 2: 0.0995 m/px at z20 in Vienna, against 0.1493 nominal Web Mercator metres.
    assert crop.pixel_size_m == pytest.approx(0.1493, abs=1e-3)
    assert imaging.ground_pixel_size_m(crop) == pytest.approx(0.0995, abs=1e-3)
    assert imaging.pixel_area_m2(crop) == pytest.approx(0.0995**2, rel=1e-2)


def test_pixel_and_lonlat_round_trip() -> None:
    crop = synthetic_crop(np.zeros((200, 200, 3), np.uint8))
    lon, lat = imaging.pixel_to_lonlat(crop, 40.0, 90.0)
    col, row = imaging.lonlat_to_pixel(crop, lon, lat)
    assert col == pytest.approx(40.5, abs=1e-6)  # pixel_to_lonlat returns the pixel *centre*
    assert row == pytest.approx(90.5, abs=1e-6)


# --------------------------------------------------------------------------------------------
# masks


def test_roof_mask_excludes_interior_ring() -> None:
    """A courtyard void is not roof. It must be punched out, not filled."""
    crop = imaging.build_crop(square_wgs84(15.0, hole_m=5.0), Path("/nonexistent"), CFG)
    mask = np.asarray(crop.roof_mask, dtype=bool)
    height, width = mask.shape
    centre = (height // 2, width // 2)
    assert not mask[centre], "interior ring was filled instead of excluded"

    solid = imaging.build_crop(square_wgs84(15.0), Path("/nonexistent"), CFG)
    assert np.asarray(solid.roof_mask)[centre], "solid outline should cover its own centre"
    # The void is ~10x10 m of a 30x30 m roof, so the holed mask must be materially smaller.
    assert mask.sum() < np.asarray(solid.roof_mask).sum() * 0.95


def test_rings_to_polygon_carries_holes_through() -> None:
    crop = synthetic_crop(np.zeros((300, 300, 3), np.uint8))
    exterior = np.array([[10, 10], [280, 10], [280, 280], [10, 280]], dtype=np.int32)
    hole = np.array([[100, 100], [180, 100], [180, 180], [100, 180]], dtype=np.int32)
    polygon = imaging.rings_to_polygon_wgs84(crop, exterior, [hole])
    assert polygon is not None
    assert len(polygon["coordinates"]) == 2, "the interior ring was dropped"
    assert imaging.rings_to_polygon_wgs84(crop, np.array([[1, 1], [2, 2]])) is None


# --------------------------------------------------------------------------------------------
# tile assembly


def test_missing_tile_is_recorded_and_zero_filled_not_raised(tmp_path: Path) -> None:
    """One absent tile must not fail a building; it must be named and left as no-data."""
    geometry = square_wgs84(15.0)
    # Discover the exact tiles this crop needs by asking for it from an empty cache.
    discovery = imaging.build_crop(geometry, tmp_path, CFG)
    needed = list(discovery.missing_tiles)
    assert len(needed) > 1, "test needs a crop spanning several tiles"

    for tile_id in needed[:-1]:
        write_tile(tmp_path, tile_id)
    crop = imaging.build_crop(geometry, tmp_path, CFG)

    assert crop.missing_tiles == (needed[-1],)
    no_data = imaging.no_data_mask(crop)
    assert no_data.any(), "the absent tile's area must be identifiable"
    assert crop.rgb[no_data].max() == 0, "absent tile area must be zero-filled"
    assert crop.rgb[~no_data].max() > 0, "present tiles must have been pasted"


def test_no_data_mask_is_empty_when_every_tile_is_cached(tmp_path: Path) -> None:
    geometry = square_wgs84(10.0)
    for tile_id in imaging.build_crop(geometry, tmp_path, CFG).missing_tiles:
        write_tile(tmp_path, tile_id)
    crop = imaging.build_crop(geometry, tmp_path, CFG)
    assert crop.missing_tiles == ()
    assert not imaging.no_data_mask(crop).any()


def test_tile_root_accepts_both_cache_layouts(tmp_path: Path) -> None:
    layer = str(CFG.threshold("imagery", "layer"))
    (tmp_path / "tiles" / layer).mkdir(parents=True)
    assert imaging.tile_root(tmp_path, layer) == tmp_path / "tiles"
    bare = tmp_path / "bare"
    (bare / layer).mkdir(parents=True)
    assert imaging.tile_root(bare, layer) == bare


# --------------------------------------------------------------------------------------------
# shadow statistics


def test_shadow_stats_on_dark_and_bright_roofs() -> None:
    limit = float(CFG.threshold("image", "shadow_luminance_threshold"))
    dark = synthetic_crop(np.full((100, 100, 3), int(limit) - 20, np.uint8))
    bright = synthetic_crop(np.full((100, 100, 3), 200, np.uint8))
    assert imaging.shadow_stats(dark, CFG)["shadow_fraction"] == 1.0
    assert imaging.shadow_stats(bright, CFG)["shadow_fraction"] == 0.0
    assert imaging.shadow_stats(bright, CFG)["mean_luminance"] == pytest.approx(200, abs=1)


def test_shadow_stats_separates_missing_imagery_from_darkness() -> None:
    """A cache gap counts as dark for the abstention gate, but is reported separately."""
    layer, zoom = CFG.threshold("imagery", "layer"), CFG.threshold("imagery", "zoom")
    size = wmts.TILE_SIZE * 2
    reference = synthetic_crop(np.zeros((size, size, 3), np.uint8))
    span = reference.pixel_size_m * wmts.TILE_SIZE
    col = int((reference.origin_x + wmts.ORIGIN_SHIFT) // span)
    row = int((wmts.ORIGIN_SHIFT - reference.origin_y) // span)
    tile_id = f"{layer}/{zoom}/{row}/{col}"

    # Paint the imagery to match where the absent tile actually lands, so the expected fractions
    # are exact rather than approximate.
    no_data = imaging.no_data_mask(synthetic_crop(reference.rgb, missing_tiles=(tile_id,)))
    rgb = np.full((size, size, 3), 200, np.uint8)
    rgb[no_data] = 0
    crop = synthetic_crop(rgb, missing_tiles=(tile_id,))

    stats = imaging.shadow_stats(crop, CFG)
    expected = float(no_data.mean())
    assert 0.0 < expected < 1.0, "the absent tile should cover part of this crop"
    assert stats["no_data_fraction"] == pytest.approx(expected, abs=0.01)
    # The gate sees the gap as dark (conservative), while the split is reported separately.
    assert stats["shadow_fraction"] == pytest.approx(expected, abs=0.01)
    assert stats["shadow_fraction_excluding_no_data"] == 0.0
    assert stats["mean_luminance"] == pytest.approx(200, abs=1)


def test_shadow_stats_abstains_on_an_empty_mask() -> None:
    crop = synthetic_crop(np.full((50, 50, 3), 200, np.uint8), mask=np.zeros((50, 50), bool))
    stats = imaging.shadow_stats(crop, CFG)
    assert stats["shadow_fraction"] is None and stats["mean_luminance"] is None


# --------------------------------------------------------------------------------------------
# integration: the committed cache, when it holds real tiles


@pytest.mark.skipif(usable_tile_cache() is None, reason="no readable tiles in the committed cache")
def test_pinned_buildings_crop_from_the_committed_cache() -> None:
    """Every pinned building must produce a crop from the offline cache, with no network."""
    import json

    cache = usable_tile_cache()
    assert cache is not None
    records = json.loads((cache / "roof_records_2025.geojson").read_text(encoding="utf-8"))
    by_id = {f["properties"]["OBJECTID"]: f for f in records["features"]}
    for building in CFG.study_area.buildings:
        geometry = by_id[building.objectid]["geometry"]
        crop = imaging.build_crop(geometry, cache, CFG)
        assert crop.rgb.ndim == 3 and crop.rgb.shape[2] == 3
        assert np.asarray(crop.roof_mask).any(), f"{building.building_id} has an empty roof mask"
        assert imaging.ground_pixel_size_m(crop) == pytest.approx(0.0995, abs=1e-3)
        imaging.shadow_stats(crop, CFG)  # must not raise, whatever the cache contains
