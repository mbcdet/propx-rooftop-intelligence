"""Vienna Orthofoto WMTS access: tile maths, tile fetching, and mosaic assembly.

The tile grid is an analytic function of zoom, so the georeferencing of an assembled
mosaic can be computed exactly without GDAL or rasterio. The affine transform is written
to a JSON sidecar next to the mosaic.

Data source: Stadt Wien, Orthofoto 2024 Wien (WMTS layer ``lb2024``), 15 cm GSD.
Licence: CC BY 4.0. Attribution: "Datenquelle: Stadt Wien - data.wien.gv.at".
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

TILE_SIZE: Final[int] = 256
# Web Mercator (EPSG:3857) semi-extent in metres.
ORIGIN_SHIFT: Final[float] = 20037508.342789244
WORLD_SIZE: Final[float] = 2 * ORIGIN_SHIFT

TILE_URL_TEMPLATE: Final[str] = (
    "https://mapsneu.wien.gv.at/wmts/{layer}/farbe/google3857/{z}/{row}/{col}.jpeg"
)
ATTRIBUTION: Final[str] = "Datenquelle: Stadt Wien - data.wien.gv.at"
USER_AGENT: Final[str] = "propx-rooftop-intelligence/0.1 (technical assessment)"

# Highest zoom advertised by the lb2024 TileMatrixSet google3857_0-21.
MAX_ZOOM: Final[int] = 21

# A mosaic is a few hundred requests; log periodically so a local run is visibly alive.
PROGRESS_EVERY: Final[int] = 50


def resolution(zoom: int) -> float:
    """Ground resolution in metres per pixel at the equator for ``zoom``."""
    return WORLD_SIZE / (TILE_SIZE * 2**zoom)


def resolution_at_latitude(zoom: int, latitude_deg: float) -> float:
    """Effective ground resolution in m/px at ``latitude_deg``.

    Web Mercator is conformal but not equal-area: the nominal resolution must be
    scaled by cos(latitude) to describe real ground sampling.
    """
    return resolution(zoom) * math.cos(math.radians(latitude_deg))


def lonlat_to_meters(lon: float, lat: float) -> tuple[float, float]:
    """WGS84 lon/lat (degrees) to EPSG:3857 metres."""
    x = lon * ORIGIN_SHIFT / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    return x, y * ORIGIN_SHIFT / 180.0


def meters_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """EPSG:3857 metres to WGS84 lon/lat (degrees)."""
    lon = x / ORIGIN_SHIFT * 180.0
    lat = y / ORIGIN_SHIFT * 180.0
    lat = 180.0 / math.pi * (2.0 * math.atan(math.exp(lat * math.pi / 180.0)) - math.pi / 2.0)
    return lon, lat


def meters_to_tile(x: float, y: float, zoom: int) -> tuple[int, int]:
    """EPSG:3857 metres to (col, row) tile indices, XYZ convention (row 0 at north)."""
    span = resolution(zoom) * TILE_SIZE
    col = math.floor((x + ORIGIN_SHIFT) / span)
    row = math.floor((ORIGIN_SHIFT - y) / span)
    return col, row


@dataclass(frozen=True)
class MosaicTransform:
    """Georeferencing for an assembled mosaic.

    ``origin_x`` / ``origin_y`` are the EPSG:3857 coordinates of the *upper-left*
    corner of pixel (0, 0). Pixel centres are offset by half a pixel.
    """

    crs: str
    zoom: int
    origin_x: float
    origin_y: float
    pixel_size: float
    width: int
    height: int
    tile_col_min: int
    tile_row_min: int
    layer: str
    attribution: str = ATTRIBUTION

    def pixel_of(self, lon: float, lat: float) -> tuple[float, float]:
        """Fractional pixel coordinates of a WGS84 position within the mosaic."""
        x, y = lonlat_to_meters(lon, lat)
        return (x - self.origin_x) / self.pixel_size, (self.origin_y - y) / self.pixel_size

    def to_json(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")


def tile_range(
    bbox_wgs84: tuple[float, float, float, float], zoom: int
) -> tuple[int, int, int, int]:
    """Inclusive (col_min, row_min, col_max, row_max) covering ``bbox_wgs84``.

    ``bbox_wgs84`` is (min_lon, min_lat, max_lon, max_lat).
    """
    min_lon, min_lat, max_lon, max_lat = bbox_wgs84
    x0, y1 = lonlat_to_meters(min_lon, max_lat)  # upper-left
    x1, y0 = lonlat_to_meters(max_lon, min_lat)  # lower-right
    col_min, row_min = meters_to_tile(x0, y1, zoom)
    col_max, row_max = meters_to_tile(x1, y0, zoom)
    return col_min, row_min, col_max, row_max


def fetch_tile(
    layer: str,
    zoom: int,
    col: int,
    row: int,
    cache_dir: Path,
    *,
    session=None,
    timeout: float = 30.0,
    retries: int = 3,
    delay: float = 0.05,
) -> Path:
    """Fetch one tile into ``cache_dir``, returning its path. Cached tiles are reused."""
    import requests

    path = cache_dir / layer / str(zoom) / str(row) / f"{col}.jpeg"
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)

    url = TILE_URL_TEMPLATE.format(layer=layer, z=zoom, row=row, col=col)
    http = session or requests.Session()
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = http.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            if response.status_code == 404:
                raise FileNotFoundError(
                    f"Tile not published: {url}. The layer may not cover this area at z{zoom}."
                )
            response.raise_for_status()
            if not response.content:
                raise ValueError(f"Empty tile body from {url}")
            path.write_bytes(response.content)
            time.sleep(delay)  # be a polite client of a public service
            return path
        except FileNotFoundError:
            raise
        except Exception as error:  # noqa: BLE001 - retried and re-raised below
            last_error = error
            logger.warning("tile fetch failed (attempt %d/%d): %s", attempt, retries, error)
            time.sleep(delay * 10 * attempt)
    raise RuntimeError(f"Could not fetch {url} after {retries} attempts") from last_error


def build_mosaic(
    bbox_wgs84: tuple[float, float, float, float],
    zoom: int,
    cache_dir: Path,
    *,
    layer: str = "lb2024",
) -> tuple[object, MosaicTransform]:
    """Fetch the tiles covering ``bbox_wgs84`` and assemble them into one image.

    Returns the PIL image and its :class:`MosaicTransform`.
    """
    import requests
    from PIL import Image

    if not 0 <= zoom <= MAX_ZOOM:
        raise ValueError(f"zoom {zoom} outside the published range 0..{MAX_ZOOM}")

    col_min, row_min, col_max, row_max = tile_range(bbox_wgs84, zoom)
    n_cols, n_rows = col_max - col_min + 1, row_max - row_min + 1
    total = n_cols * n_rows
    logger.info("mosaic %s z%d: %d x %d tiles (%d total)", layer, zoom, n_cols, n_rows, total)

    canvas = Image.new("RGB", (n_cols * TILE_SIZE, n_rows * TILE_SIZE))
    done = 0
    # One session for the whole mosaic: connection reuse matters over ~400 requests.
    with requests.Session() as session:
        for row in range(row_min, row_max + 1):
            for col in range(col_min, col_max + 1):
                tile_path = fetch_tile(layer, zoom, col, row, cache_dir, session=session)
                with Image.open(tile_path) as tile:
                    canvas.paste(
                        tile.convert("RGB"),
                        ((col - col_min) * TILE_SIZE, (row - row_min) * TILE_SIZE),
                    )
                done += 1
                if done % PROGRESS_EVERY == 0 or done == total:
                    logger.info("  %s z%d: %d/%d tiles", layer, zoom, done, total)

    pixel_size = resolution(zoom)
    span = pixel_size * TILE_SIZE
    transform = MosaicTransform(
        crs="EPSG:3857",
        zoom=zoom,
        origin_x=col_min * span - ORIGIN_SHIFT,
        origin_y=ORIGIN_SHIFT - row_min * span,
        pixel_size=pixel_size,
        width=canvas.width,
        height=canvas.height,
        tile_col_min=col_min,
        tile_row_min=row_min,
        layer=layer,
    )
    return canvas, transform
