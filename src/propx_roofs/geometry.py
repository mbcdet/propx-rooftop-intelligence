"""Geometry in two frames, only one of which is allowed to publish a number.

Reconnaissance compared overlaps in a cheap local ``cos(latitude)`` scaling of degrees
(``tools/verify_join.py``). That frame is adequate for a ratio and wrong for a published
area — it carries ~0.743% area error (measured) over a study-area-sized box (design section 2,
Amendment B3). The separation is therefore enforced by construction rather than by
comment discipline:

* :func:`to_metric` is the only function in the repository that returns a
  :class:`~propx_roofs.types.MetricGeometry`, and it refuses a CRS that is not projected.
* :func:`local_diagnostic_scale` returns a bare shapely geometry. Nothing downstream will
  accept it as a measurement, because measurements travel as ``MetricGeometry``.

All azimuths in this module are measured from **grid north of the projected CRS**, not
from true north. At Vienna in EPSG:31256 (central meridian 16 deg 20') meridian
convergence is under 0.05 deg, which is far below the angular resolution of anything we
derive from 15 cm imagery — but it is a grid bearing, and the field names say so.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, Final

import shapely
from pyproj import CRS, Transformer
from shapely.affinity import scale
from shapely.geometry import Polygon, box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from .types import MetricGeometry

WGS84: Final[str] = "EPSG:4326"
DEFAULT_METRIC_CRS: Final[str] = "EPSG:31256"

# The reconnaissance constants, repeated verbatim so diagnostic numbers computed here stay
# comparable with the ones already recorded in outputs/recon/.
_M_PER_DEG_LAT: Final[float] = 110574.0
_M_PER_DEG_LON_EQUATOR: Final[float] = 111320.0

# A bbox edge that is straight in WGS84 is not straight in a projected CRS, so the corners
# alone would understate the extent. Sampling at ~25 m keeps the round-trip error well
# below a metre for a few dozen extra vertices.
_BBOX_SEGMENT_M: Final[float] = 25.0
_BBOX_SEGMENT_DEG: Final[float] = 2.5e-4


@lru_cache(maxsize=8)
def projected_crs(crs: str) -> CRS:
    """Parse ``crs`` and reject anything that cannot carry a metric measurement.

    A geographic CRS would silently yield square degrees, and square degrees look like a
    plausible small number rather than an error.
    """
    parsed = CRS.from_user_input(crs)
    if not parsed.is_projected:
        kind = "geographic" if parsed.is_geographic else "non-projected"
        raise ValueError(
            f"{crs} ({parsed.name}) is a {kind} CRS and cannot produce a metric value. "
            f"Published areas, distances and azimuths come from a projected CRS "
            f"({DEFAULT_METRIC_CRS} for this pipeline); the local cos(latitude) frame is "
            f"diagnostic only (design section 2, Amendment B3)."
        )
    return parsed


@lru_cache(maxsize=16)
def transformer(source_crs: str, target_crs: str) -> Transformer:
    """Cached ``always_xy=True`` transformer.

    Building a Transformer parses the PROJ database, which is expensive enough to matter
    when every roof record projects itself and every candidate unit.
    """
    return Transformer.from_crs(
        CRS.from_user_input(source_crs), CRS.from_user_input(target_crs), always_xy=True
    )


def as_shapely(geom: Any) -> BaseGeometry:
    """Accept a shapely geometry or a GeoJSON mapping (``RoofRecord.geometry`` is a dict)."""
    if isinstance(geom, BaseGeometry):
        return geom
    return shape(geom)


def _polygons(geom: BaseGeometry) -> list[Polygon]:
    """Polygonal members of ``geom``; non-polygonal input contributes no area."""
    if isinstance(geom, Polygon):
        return [] if geom.is_empty else [geom]
    parts = getattr(geom, "geoms", ())
    return [g for g in parts if isinstance(g, Polygon) and not g.is_empty]


def to_metric(
    geom_wgs84: Any, crs: str = DEFAULT_METRIC_CRS
) -> tuple[BaseGeometry, MetricGeometry]:
    """Project ``geom_wgs84`` into ``crs`` and describe it.

    Returns the projected shapely geometry (for further metric work such as intersections)
    and the :class:`MetricGeometry` contract carrying the measurements. This is the only
    route to a published metric value.

    ``area_m2`` excludes interior rings — a courtyard void is not roof. The void area is
    reported alongside it so the exclusion is visible rather than assumed.
    """
    projected_crs(crs)  # refuse a geographic CRS before any number exists
    geom = as_shapely(geom_wgs84)
    projected = shapely_transform(transformer(WGS84, crs).transform, geom)
    return projected, MetricGeometry(
        crs=crs,
        area_m2=area_m2(projected),
        n_polygons=n_polygons(projected),
        n_interior_rings=n_interior_rings(projected),
        interior_ring_area_m2=interior_ring_area_m2(projected),
    )


def area_m2(geom_metric: BaseGeometry) -> float:
    """Planimetric area of an already-projected geometry, interior rings excluded.

    Shapely subtracts holes for us; the exclusion is asserted by a test rather than
    trusted, because a courtyard void counted as roof would inflate every derived figure.
    Passing an unprojected geometry here yields square degrees, so reach it through
    :func:`to_metric`.
    """
    return float(sum(p.area for p in _polygons(geom_metric)))


def n_polygons(geom_metric: BaseGeometry) -> int:
    """Number of disjoint polygonal parts — a multipart outline is not one roof plane."""
    return len(_polygons(geom_metric))


def n_interior_rings(geom_metric: BaseGeometry) -> int:
    """Number of interior rings across all parts (courtyard voids, light wells)."""
    return sum(len(p.interiors) for p in _polygons(geom_metric))


def interior_ring_area_m2(geom_metric: BaseGeometry) -> float:
    """Total area of interior rings, reported separately from :func:`area_m2`."""
    return float(
        sum(Polygon(ring).area for p in _polygons(geom_metric) for ring in p.interiors)
    )


def _azimuth_from_delta(dx: float, dy: float) -> float:
    """Compass azimuth of a grid vector, folded to ``[0, 180)``.

    ``atan2(dx, dy)`` rather than the usual ``atan2(dy, dx)``: compass bearings run
    clockwise from the +y (grid north) axis, not counter-clockwise from +x.
    """
    if dx == 0.0 and dy == 0.0:
        raise ValueError("azimuth of a zero-length vector is undefined")
    return math.degrees(math.atan2(dx, dy)) % 180.0


def azimuth_deg(
    p0: tuple[float, float], p1: tuple[float, float], crs: str = DEFAULT_METRIC_CRS
) -> float:
    """Azimuth of the line ``p0``-``p1`` in degrees clockwise from grid north of ``crs``.

    ``p0`` and ``p1`` are ``(lon, lat)`` in WGS84. The result is normalised to
    ``[0, 180)`` because a ridge line or a footprint axis is **undirected**: north-south
    and south-north are the same orientation, and reporting 0 vs 180 for the same ridge
    would look like a disagreement. 0 = grid north-south, 90 = grid east-west.

    Grid north, not true north — see the module docstring.
    """
    fwd = transformer(WGS84, crs)
    projected_crs(crs)
    x0, y0 = fwd.transform(p0[0], p0[1])
    x1, y1 = fwd.transform(p1[0], p1[1])
    return _azimuth_from_delta(x1 - x0, y1 - y0)


def min_rotated_rect_axis(geom_metric: BaseGeometry) -> tuple[float, float, float]:
    """Long-axis azimuth, long side and short side of the minimum rotated rectangle.

    Input must already be projected (via :func:`to_metric`), so the two lengths are metres.
    The azimuth follows :func:`azimuth_deg`: grid north, ``[0, 180)``. This is the
    building's long axis, **not** a roof slope aspect. On a near-square footprint the two
    sides are nearly equal and the reported axis is correspondingly arbitrary, which is why
    the caller gets both lengths and can decide whether the axis means anything.
    """
    rect = geom_metric.minimum_rotated_rectangle
    if not isinstance(rect, Polygon) or rect.is_empty:
        raise ValueError(
            f"geometry is degenerate ({rect.geom_type}); it has no rotated-rectangle axis"
        )
    (x0, y0), (x1, y1), (x2, y2) = list(rect.exterior.coords)[:3]
    first = (x1 - x0, y1 - y0)
    second = (x2 - x1, y2 - y1)
    len_first = math.hypot(*first)
    len_second = math.hypot(*second)
    if len_first >= len_second:
        return _azimuth_from_delta(*first), len_first, len_second
    return _azimuth_from_delta(*second), len_second, len_first


def buffer_bbox_m(
    bbox_wgs84: tuple[float, float, float, float],
    buffer_m: float,
    crs: str = DEFAULT_METRIC_CRS,
) -> tuple[float, float, float, float]:
    """Expand ``(min_lon, min_lat, max_lon, max_lat)`` by ``buffer_m`` metres.

    The expansion happens in ``crs`` so the buffer is really metres, not a degree offset
    that would be ~1.5x wider east-west than north-south at Vienna's latitude. Used for the
    buffered FMZK fetch (design section 1.4, item 1): reconnaissance measured 22 of 54
    matched units truncated by an unbuffered bbox, which depresses IoU for reasons that have
    nothing to do with the data.

    ``buffer_m=0`` is only approximately the identity: a WGS84-aligned rectangle is slightly
    rotated in the projected CRS, so its projected bounding box is a little wider than the
    original. At Vienna over a ~500 m box that is a few tens of centimetres, and it errs
    outwards — a fetch that is marginally too big, never too small.
    """
    if buffer_m < 0:
        raise ValueError(
            f"buffer_m must be >= 0, got {buffer_m}; a negative buffer would silently "
            f"shrink the fetch area and re-introduce the truncation it exists to prevent"
        )
    fwd = transformer(WGS84, crs)
    inv = transformer(crs, WGS84)
    projected_crs(crs)

    outline = shapely.segmentize(box(*bbox_wgs84), max_segment_length=_BBOX_SEGMENT_DEG)
    min_x, min_y, max_x, max_y = shapely_transform(fwd.transform, outline).bounds
    expanded = box(min_x - buffer_m, min_y - buffer_m, max_x + buffer_m, max_y + buffer_m)
    back = shapely_transform(
        inv.transform, shapely.segmentize(expanded, max_segment_length=_BBOX_SEGMENT_M)
    )
    min_lon, min_lat, max_lon, max_lat = back.bounds
    return (min_lon, min_lat, max_lon, max_lat)


def local_diagnostic_scale(geom: Any, lat0: float) -> BaseGeometry:
    """**DIAGNOSTIC ONLY.** Scale lon/lat degrees into an approximately isotropic frame.

    This is the reconnaissance frame from ``tools/verify_join.py``, kept so its published
    numbers stay reproducible and so the test that proves the two frames are *not*
    interchangeable has something to compare against.

    It deliberately returns a bare shapely geometry and never a
    :class:`~propx_roofs.types.MetricGeometry`: a small-angle approximation carrying
    ~0.743% area error (measured) may not produce ``roof_area_m2`` or any other published value
    (design section 2, Amendment B3). Use :func:`to_metric` for anything that is reported.
    """
    return scale(
        as_shapely(geom),
        xfact=math.cos(math.radians(lat0)) * _M_PER_DEG_LON_EQUATOR,
        yfact=_M_PER_DEG_LAT,
        origin=(0, 0),
    )
