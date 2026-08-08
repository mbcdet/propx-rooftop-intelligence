"""Per-building image crops, roof masks and shadow statistics — offline, from the tile cache.

This module is the only place the image stage touches pixels-to-ground georeferencing, so the
tile maths lives in exactly one place: :mod:`propx_roofs.sources.wmts` (reused, never
reimplemented here). Nothing in this file makes a network call. A run must be reproducible from
the committed cache, so a missing tile is *recorded* and its area filled with zeros rather than
raising — one absent tile at the edge of a 12 m margin must not fail a building.

**Two metre units exist here and confusing them would corrupt every area.** ``ImageCrop`` is
georeferenced in EPSG:3857 (that is the actual tile grid), so ``ImageCrop.pixel_size_m`` is in
*Web Mercator* metres and is what maps a pixel back to a coordinate. Ground sampling is
``pixel_size_m * cos(latitude)`` — 0.0995 m at z20 in Vienna, as design section 2 states — and
that is what an ``m2`` threshold means. :func:`ground_pixel_size_m` is the only route to the
second one, and every attribute threshold expressed in metres goes through it.

Data source: Stadt Wien, Orthofoto 2024 Wien (WMTS ``lb2024``), 15 cm GSD, CC BY 4.0,
"Datenquelle: Stadt Wien - data.wien.gv.at".
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from .geometry import as_shapely
from .sources import wmts
from .types import ImageCrop

_REQUIRED = object()  # sentinel: "no default", distinguishable from a legitimate None


def threshold(cfg: Any, *path: str, default: Any = _REQUIRED) -> Any:
    """Read a nested threshold from a :class:`~propx_roofs.config.Config` or plain mapping.

    Missing key and no ``default`` raises: a threshold that silently defaults to zero is worse
    than a crash, because it ships. ``default`` is used only for the algorithm-shape parameters
    documented in this package (see ``segment/__init__.py`` and ``attributes/__init__.py``).
    Most published decision thresholds live in ``configs/pipeline.yaml``, but **not all**: some
    values in those two dicts also decide published output (``warn_iou_below``,
    ``solar_internal_texture_min``, ``ridge_plane_contrast_min``). They are fingerprinted by
    ``config.algorithm_parameters_hash`` rather than by ``config_hash``.
    """
    node: Any = getattr(cfg, "thresholds", cfg)
    for key in path:
        if not isinstance(node, dict) or key not in node:
            if default is _REQUIRED:
                raise KeyError(f"missing threshold {'.'.join(path)} in the pipeline config")
            return default
        node = node[key]
    return node


def tile_root(cache_dir: Path | str, layer: str) -> Path:
    """Resolve the directory holding ``<layer>/<zoom>/<row>/<col>.jpeg``.

    The committed cache nests tiles under ``data/cache/<area>/tiles/``, while
    :func:`propx_roofs.sources.wmts.fetch_tile` writes relative to a bare tile root. Accepting
    either means a caller can pass ``StudyArea.cache_dir`` without knowing the difference.
    """
    root = Path(cache_dir)
    if (root / layer).is_dir():
        return root
    if (root / "tiles" / layer).is_dir():
        return root / "tiles"
    # Nothing cached yet: return the conventional location so the missing tiles are *named*
    # in ImageCrop.missing_tiles rather than hidden behind a directory-not-found error.
    return root / "tiles"


def _polygon_parts(geom: BaseGeometry) -> list[Polygon]:
    """Polygonal members of ``geom``; a non-polygonal input contributes no roof area."""
    if isinstance(geom, Polygon):
        return [] if geom.is_empty else [geom]
    return [g for g in getattr(geom, "geoms", ()) if isinstance(g, Polygon) and not g.is_empty]


def _read_tile(path: Path) -> np.ndarray | None:
    """Decode one cached tile as BGR, or ``None`` if absent or undecodable."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    tile = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if tile is None or tile.shape[0] != wmts.TILE_SIZE or tile.shape[1] != wmts.TILE_SIZE:
        return None  # a truncated JPEG is a cache problem, reported like a missing tile
    return tile


def _assemble(
    root: Path, layer: str, zoom: int, col_min: int, row_min: int, col_max: int, row_max: int
) -> tuple[np.ndarray, tuple[str, ...]]:
    """Paste cached tiles into one BGR canvas. Absent tiles stay zero and are named."""
    n_cols = col_max - col_min + 1
    n_rows = row_max - row_min + 1
    canvas = np.zeros((n_rows * wmts.TILE_SIZE, n_cols * wmts.TILE_SIZE, 3), dtype=np.uint8)
    missing: list[str] = []
    for row in range(row_min, row_max + 1):
        for col in range(col_min, col_max + 1):
            tile = _read_tile(root / layer / str(zoom) / str(row) / f"{col}.jpeg")
            if tile is None:
                missing.append(f"{layer}/{zoom}/{row}/{col}")
                continue
            top = (row - row_min) * wmts.TILE_SIZE
            left = (col - col_min) * wmts.TILE_SIZE
            canvas[top : top + wmts.TILE_SIZE, left : left + wmts.TILE_SIZE] = tile
    return canvas, tuple(missing)


def build_crop(roof_geom_wgs84: Any, cache_dir: Path | str, cfg: Any) -> ImageCrop:
    """Assemble the image window for one roof outline from the offline tile cache.

    ``roof_geom_wgs84`` is a shapely geometry or a GeoJSON mapping in EPSG:4326 (the shape of
    :attr:`propx_roofs.types.RoofRecord.geometry`). The window is the outline's bounding box
    expanded by ``imagery.crop_margin_m`` *ground* metres, which is why the margin is divided by
    cos(latitude) before it is applied in Web Mercator.

    Only the tiles the window touches are read. No network access, ever: a missing tile is
    recorded in ``ImageCrop.missing_tiles`` and its pixels are left zero.
    """
    layer = str(threshold(cfg, "imagery", "layer"))
    zoom = int(threshold(cfg, "imagery", "zoom"))
    margin_m = float(threshold(cfg, "imagery", "crop_margin_m"))

    geom = as_shapely(roof_geom_wgs84)
    if geom.is_empty:
        raise ValueError("cannot build an image crop for an empty geometry")
    min_lon, min_lat, max_lon, max_lat = geom.bounds

    pixel_size = wmts.resolution(zoom)  # EPSG:3857 metres per pixel
    lat_centre = 0.5 * (min_lat + max_lat)
    margin_3857 = margin_m / math.cos(math.radians(lat_centre))

    west, south = wmts.lonlat_to_meters(min_lon, min_lat)
    east, north = wmts.lonlat_to_meters(max_lon, max_lat)
    west, south = west - margin_3857, south - margin_3857
    east, north = east + margin_3857, north + margin_3857

    w_lon, s_lat = wmts.meters_to_lonlat(west, south)
    e_lon, n_lat = wmts.meters_to_lonlat(east, north)
    col_min, row_min, col_max, row_max = wmts.tile_range((w_lon, s_lat, e_lon, n_lat), zoom)

    root = tile_root(cache_dir, layer)
    canvas, missing = _assemble(root, layer, zoom, col_min, row_min, col_max, row_max)

    span = pixel_size * wmts.TILE_SIZE
    canvas_x = col_min * span - wmts.ORIGIN_SHIFT
    canvas_y = wmts.ORIGIN_SHIFT - row_min * span

    # Cut the tile-aligned canvas down to the requested window, so the margin is the configured
    # one rather than however much a 256 px tile boundary happened to give.
    col0 = max(0, int(math.floor((west - canvas_x) / pixel_size)))
    row0 = max(0, int(math.floor((canvas_y - north) / pixel_size)))
    col1 = min(canvas.shape[1], int(math.ceil((east - canvas_x) / pixel_size)))
    row1 = min(canvas.shape[0], int(math.ceil((canvas_y - south) / pixel_size)))
    window = canvas[row0:row1, col0:col1]

    origin_x = canvas_x + col0 * pixel_size
    origin_y = canvas_y - row0 * pixel_size
    rgb = cv2.cvtColor(np.ascontiguousarray(window), cv2.COLOR_BGR2RGB)
    mask = rasterise_mask(geom, origin_x, origin_y, pixel_size, rgb.shape[:2])

    return ImageCrop(
        rgb=rgb,
        roof_mask=mask,
        origin_x=origin_x,
        origin_y=origin_y,
        pixel_size_m=pixel_size,
        zoom=zoom,
        crs="EPSG:3857",
        missing_tiles=missing,
    )


def rasterise_mask(
    geom_wgs84: Any,
    origin_x: float,
    origin_y: float,
    pixel_size_m: float,
    shape: tuple[int, int],
) -> np.ndarray:
    """Rasterise an outline to a boolean mask, **interior rings excluded**.

    Exteriors of every part are filled first and interior rings are then punched out, so a
    courtyard void is not roof. Doing it in that order (rather than per part) is safe because a
    ring of one part never contains another part in valid multipart building data — and if it
    did, the void would still win, which is the conservative direction.
    """
    geom = as_shapely(geom_wgs84)
    mask = np.zeros(shape, dtype=np.uint8)
    parts = _polygon_parts(geom)
    if not parts:
        return mask.astype(bool)

    def ring_px(ring: Any) -> np.ndarray:
        coords = np.asarray(ring.coords, dtype=np.float64)
        xs, ys = _lonlat_to_meters_array(coords[:, 0], coords[:, 1])
        cols = (xs - origin_x) / pixel_size_m
        rows = (origin_y - ys) / pixel_size_m
        return np.rint(np.column_stack([cols, rows])).astype(np.int32)

    cv2.fillPoly(mask, [ring_px(p.exterior) for p in parts], 255)
    holes = [ring_px(r) for p in parts for r in p.interiors]
    if holes:
        cv2.fillPoly(mask, holes, 0)
    return mask.astype(bool)


def _lonlat_to_meters_array(lons: np.ndarray, lats: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised form of :func:`propx_roofs.sources.wmts.lonlat_to_meters`.

    Identical formula, applied to whole rings at once; a ring can carry a few thousand vertices
    and a Python-level loop over them shows up in the per-building budget.
    """
    x = lons * wmts.ORIGIN_SHIFT / 180.0
    y = np.log(np.tan((90.0 + lats) * np.pi / 360.0)) / (np.pi / 180.0)
    return x, y * wmts.ORIGIN_SHIFT / 180.0


def crop_latitude(crop: ImageCrop) -> float:
    """Latitude of the crop centre, used for the Web-Mercator-to-ground scale factor."""
    centre_y = crop.origin_y - 0.5 * crop.rgb.shape[0] * crop.pixel_size_m
    _, lat = wmts.meters_to_lonlat(crop.origin_x, centre_y)
    return lat


def ground_pixel_size_m(crop: ImageCrop) -> float:
    """Ground sampling distance of one pixel, in real metres.

    ``ImageCrop.pixel_size_m`` is Web Mercator metres, which at Vienna's latitude overstates
    ground distance by 1/cos(48.2 deg) ~ 1.5x. Every threshold expressed in m or m2 must use
    this value: at z20 it is 0.0995 m, matching design section 2. Note the *information* content
    stays at the native 15 cm GSD regardless of the finer pixel grid.
    """
    return crop.pixel_size_m * math.cos(math.radians(crop_latitude(crop)))


def pixel_area_m2(crop: ImageCrop) -> float:
    """Ground area of one pixel in m2."""
    return ground_pixel_size_m(crop) ** 2


def px_from_m(crop: ImageCrop, metres: float, minimum: int = 1) -> int:
    """Convert a ground distance to a whole number of pixels, at least ``minimum``.

    Kernel radii and simplification tolerances are specified in metres so they mean the same
    thing at any zoom; this is the only conversion.
    """
    return max(minimum, int(round(metres / ground_pixel_size_m(crop))))


def luminance(crop: ImageCrop) -> np.ndarray:
    """BT.601 grey channel of the crop, uint8."""
    return cv2.cvtColor(crop.rgb, cv2.COLOR_RGB2GRAY)


def lonlat_to_pixel(crop: ImageCrop, lon: float, lat: float) -> tuple[float, float]:
    """Fractional (col, row) of a WGS84 position inside ``crop``."""
    x, y = wmts.lonlat_to_meters(lon, lat)
    return (x - crop.origin_x) / crop.pixel_size_m, (crop.origin_y - y) / crop.pixel_size_m


def pixel_to_lonlat(crop: ImageCrop, col: float, row: float) -> tuple[float, float]:
    """WGS84 (lon, lat) of a pixel *centre* in ``crop``.

    The half-pixel offset matters: without it every CV polygon is biased half a pixel
    north-west, which is ~5 cm — small, but it would be a systematic bias in the agreement
    metrics rather than noise.
    """
    x = crop.origin_x + (col + 0.5) * crop.pixel_size_m
    y = crop.origin_y - (row + 0.5) * crop.pixel_size_m
    return wmts.meters_to_lonlat(x, y)


def rings_to_polygon_wgs84(
    crop: ImageCrop, exterior: np.ndarray, holes: Sequence[np.ndarray] = ()
) -> dict[str, Any] | None:
    """Convert pixel rings (Nx1x2 or Nx2, x=col) to a GeoJSON Polygon in EPSG:4326.

    ``None`` for an exterior with fewer than three distinct points, because a two-point "polygon"
    is a failure to segment and must travel as a failure, not as a degenerate geometry. Holes are
    carried through: a courtyard void is not roof, and dropping it would inflate the candidate's
    area against an authoritative outline that correctly excludes it.
    """
    rings: list[list[list[float]]] = []
    for index, ring_px in enumerate([exterior, *holes]):
        pts = np.asarray(ring_px, dtype=np.float64).reshape(-1, 2)
        if len(pts) < 3:
            if index == 0:
                return None
            continue  # a degenerate hole is dropped; a degenerate exterior is a failure
        ring = [pixel_to_lonlat(crop, float(c), float(r)) for c, r in pts]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        if len(ring) < 4:
            if index == 0:
                return None
            continue
        rings.append([[lon, lat] for lon, lat in ring])
    return {"type": "Polygon", "coordinates": rings}


def contour_to_polygon_wgs84(crop: ImageCrop, contour: np.ndarray) -> dict[str, Any] | None:
    """Single-ring form of :func:`rings_to_polygon_wgs84`."""
    return rings_to_polygon_wgs84(crop, contour)


def no_data_mask(crop: ImageCrop) -> np.ndarray:
    """Pixels with no cached imagery behind them, reconstructed exactly from ``missing_tiles``.

    A missing tile is filled with zeros, which is indistinguishable from a very dark pixel by
    value alone — and "very dark" is exactly the signature ``attributes/solar.py`` looks for. So
    the no-data region is recovered from the *tile identifiers*, which is exact, rather than by
    testing for black pixels, which would also flag genuinely black imagery. Detectors must
    exclude these pixels: a cache gap is absence of observation, not observation of absence.
    """
    mask = np.zeros(crop.rgb.shape[:2], dtype=bool)
    span = crop.pixel_size_m * wmts.TILE_SIZE
    for tile_id in crop.missing_tiles:
        _, _, row, col = tile_id.split("/")
        tile_x = int(col) * span - wmts.ORIGIN_SHIFT
        tile_y = wmts.ORIGIN_SHIFT - int(row) * span
        col0 = int(round((tile_x - crop.origin_x) / crop.pixel_size_m))
        row0 = int(round((crop.origin_y - tile_y) / crop.pixel_size_m))
        mask[max(0, row0) : row0 + wmts.TILE_SIZE, max(0, col0) : col0 + wmts.TILE_SIZE] = True
    return mask


def shadow_threshold_luminance(crop: ImageCrop, cfg: Any) -> tuple[float, float | None]:
    """Effective shadow luminance threshold for this crop: ``(threshold, lit_reference)``.

    The absolute threshold alone (``image.shadow_luminance_threshold``) was measured inactive on
    this imagery — cast shadow in a sun-lit orthophoto sits at luminance ~75–95, far above 55 —
    so the live test is *relative*: a pixel is shadow when it is darker than
    ``shadow_relative_ratio`` times the roof's own lit reference (the
    ``shadow_reference_percentile`` of roof pixels that have imagery). The ratio comes from
    illumination physics, not from any building: a sky-only (shadowed) surface receives
    ~15–25% of the irradiance a sun-plus-sky surface receives at March sun elevations, which
    gamma-encodes to pixel ratios of ~0.42–0.53. See ``configs/pipeline.yaml`` for the full
    rationale, including why self-shaded pitched planes (encoded ratios 0.55–0.63) stay lit.

    The result is clipped to ``[shadow_luminance_threshold, shadow_luminance_ceiling]``: below
    the floor nothing is judgeable regardless of context, and above the ceiling a pixel carries
    enough absolute signal to judge regardless of how bright its neighbours are. The lit
    reference is ``None`` when no roof pixel has imagery; the floor is returned then.
    """
    floor = float(threshold(cfg, "image", "shadow_luminance_threshold"))
    ratio = float(threshold(cfg, "image", "shadow_relative_ratio"))
    percentile = float(threshold(cfg, "image", "shadow_reference_percentile"))
    ceiling = float(threshold(cfg, "image", "shadow_luminance_ceiling"))
    mask = np.asarray(crop.roof_mask, dtype=bool)
    with_data = mask & ~no_data_mask(crop)
    if not with_data.any():
        return floor, None
    reference = float(np.percentile(luminance(crop)[with_data], percentile))
    return min(ceiling, max(floor, ratio * reference)), reference


def shadow_mask(crop: ImageCrop, cfg: Any) -> np.ndarray:
    """Roof pixels too dark to judge colour or texture on, by the adaptive threshold.

    Only pixels that *have* imagery are marked: a cache gap is absence of observation, not
    observation of darkness, and travels separately via :func:`no_data_mask`. Callers deciding
    judgeability must exclude both.
    """
    limit, _ = shadow_threshold_luminance(crop, cfg)
    mask = np.asarray(crop.roof_mask, dtype=bool)
    return mask & ~no_data_mask(crop) & (luminance(crop) < limit)


def shadow_stats(crop: ImageCrop, cfg: Any) -> dict[str, Any]:
    """Luminance quality of the roof interior — the gate every abstention decision reads.

    ``shadow_fraction`` is the share of **all** roof-mask pixels that are either below the
    adaptive shadow threshold (see :func:`shadow_threshold_luminance`) or covered by no cached
    imagery, so a cache gap counts as dark and pushes the observation towards abstention. That
    is the conservative direction and it is deliberate. ``no_data_fraction`` and
    ``shadow_fraction_excluding_no_data`` separate the two causes, so a reader can tell a
    shadowed courtyard from a missing tile.

    ``mean_luminance`` is measured over roof pixels that *have* imagery: averaging in fabricated
    zeros would report a number that describes our cache rather than the roof. Every fraction is
    ``None`` when there are no roof pixels at all — "cannot judge", not "no shadow".
    """
    limit, lit_reference = shadow_threshold_luminance(crop, cfg)
    floor = float(threshold(cfg, "image", "shadow_luminance_threshold"))
    mask = np.asarray(crop.roof_mask, dtype=bool)
    grey = luminance(crop)
    n = int(mask.sum())
    if n == 0:
        return {
            "roof_pixel_count": 0,
            "shadow_fraction": None,
            "mean_luminance": None,
            "no_data_fraction": None,
            "shadow_fraction_excluding_no_data": None,
            "shadow_luminance_threshold": floor,
            "shadow_luminance_threshold_effective": limit,
            "shadow_lit_reference_luminance": lit_reference,
            "missing_tile_count": len(crop.missing_tiles),
            "note": "no roof pixels inside the crop; luminance quality is undefined",
        }
    no_data = no_data_mask(crop) & mask
    with_data = mask & ~no_data
    n_data = int(with_data.sum())
    shadowed = with_data & (grey < limit)
    return {
        "roof_pixel_count": n,
        # NOT the published area. This counts raster-mask pixels and scales them by the local
        # cos(latitude) ground approximation. It differs from attributes.roof_area_m2 (projected
        # into EPSG:31256) for two independent reasons -- rasterising a polygon onto a pixel grid,
        # and the approximate measurement frame -- and the two effects are not separated here, so
        # no bound on the difference is asserted. Named and labelled explicitly because the two
        # were previously both called "roof_area_m2" and could be mistaken for each other.
        "roof_mask_area_m2_approx": round(n * pixel_area_m2(crop), 1),
        "roof_mask_area_measurement_frame": "local_ground_scale_cos_latitude",
        "roof_mask_area_source_crs": "EPSG:3857",
        "roof_mask_area_note": (
            "approximate raster-mask diagnostic from the image grid, not the published "
            "authoritative area; attributes.roof_area_m2 is the published figure and is computed "
            "in EPSG:31256. The two may differ because of rasterisation onto the pixel grid and "
            "because of the approximate local ground-scale measurement frame"
        ),
        # Shadow-or-no-data over the whole outline: the conservative gate figure.
        "shadow_fraction": round(float((int(shadowed.sum()) + int(no_data.sum())) / n), 4),
        "mean_luminance": round(float(grey[with_data].mean()), 2) if n_data else None,
        "no_data_fraction": round(float(no_data.sum() / n), 4),
        "shadow_fraction_excluding_no_data": (
            round(float(shadowed.sum() / n_data), 4) if n_data else None
        ),
        "shadow_luminance_threshold": floor,
        # The adaptive threshold actually applied, and the lit reference it came from, so a
        # published shadow_fraction is reproducible from the record itself.
        "shadow_luminance_threshold_effective": round(limit, 1),
        "shadow_lit_reference_luminance": (
            None if lit_reference is None else round(lit_reference, 1)
        ),
        # Measured luminance, published so shadow_fraction is self-evidencing. A reader can
        # see how far the darkest roof pixels actually sit from the configured threshold
        # instead of taking a 0.0 on trust: visibly shadowed roof can still be well above it.
        "min_luminance": int(grey[with_data].min()) if n_data else None,
        "p01_luminance": round(float(np.percentile(grey[with_data], 1)), 1) if n_data else None,
        "p05_luminance": round(float(np.percentile(grey[with_data], 5)), 1) if n_data else None,
        "missing_tile_count": len(crop.missing_tiles),
    }
