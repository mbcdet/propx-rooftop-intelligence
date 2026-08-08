"""Build and verify the committed study-area cache. **This is the only network step.**

Design intent: ``make run`` is offline and deterministic, so everything it reads must already
be on disk and provably correct. This tool fetches that data and then re-reads every byte of
it. Nothing else in the pipeline touches the network.

Why this is a fetch rather than a copy from ``outputs/recon/``:

* The reconnaissance FMZK extract was fetched for the **unbuffered** study bbox. The join
  policy (design section 1.4 rule 1) requires FMZK for the bbox expanded by
  ``join.fmzk_buffer_m``, and ``units.load_units`` reports ``fmzk_fetch_buffer_m`` in the
  published output. Copying unbuffered data while publishing a 150 m buffer would be false
  provenance, so the buffered extent is fetched for real and recorded in the manifest.
* The buffered subset is kept **whole**. Trimming it by an approximate degree buffer would
  drop exactly the neighbouring units that make best-versus-second-best evidence meaningful.

Content integrity (RTI-011): the manifest carries a sha256 for **every** raw input file — each
vector GeoJSON and each WMTS tile — plus per-layer feature counts and bboxes, and ``verify``
recomputes and compares all of them. A fresh ``cache-build`` writes those hashes at fetch time.
For a cache fetched before hashing existed there is an explicit offline migration,
``cache-hash``, which pins the *currently committed* bytes forward from the day it runs; the
manifest records that basis honestly rather than claiming fetch-time verification.

Geometry policy (RTI-026): ``verify`` checks per-feature geometry validity, expected geometry
types per layer, plausible WGS84 Vienna coordinates, and required identifier fields.
**Verification FAILS on invalid geometry — nothing is repaired.** A repair (``buffer(0)``,
``make_valid``) changes the shape that every downstream area, IoU and containment figure is
computed from, so it would need an explicit, recorded decision; a verifier must never make it
silently.

    propx-roofs cache-build                    # fetch, then verify   (needs network)
    propx-roofs cache-verify                   # verify only          (offline)
    propx-roofs cache-hash                     # offline hash migration for an existing cache
    python3 -m propx_roofs.cache_build         # same fetch+verify, without the entry point

``tools/build_cache.py`` remains as a thin shim that delegates here, so the historical
invocation ``python3 tools/build_cache.py`` keeps working from a checkout.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, geometry, units
from .sources import wfs, wmts

TILE_PX = 256

# Fetched for the plain study bbox: these are per-building lookups, not neighbourhood context.
BBOX_LAYERS = {
    "roof_records_2025.geojson": wfs.LAYER_ROOF_RECORD_2025,
    "typology.geojson": wfs.LAYER_TYPOLOGY,
    "building_info.geojson": wfs.LAYER_BUILDING_INFO,
}
# Fetched for the BUFFERED bbox, because a truncated unit produces a depressed IoU that has
# nothing to do with the data.
FMZK_FILE = "fmzk_parts.geojson"

# RTI-026: what each cached vector layer must contain. Geometry types as observed on the live
# service (building_info is a point layer; everything else is polygonal), and the identifier
# field the pipeline joins or reports on — absent or null, the feature is unusable.
VECTOR_LAYER_CHECKS: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {
    "roof_records_2025.geojson": (frozenset({"Polygon", "MultiPolygon"}), ("OBJECTID",)),
    "typology.geojson": (frozenset({"Polygon", "MultiPolygon"}), ("OBJECTID",)),
    "building_info.geojson": (frozenset({"Point"}), ("OBJECTID",)),
    FMZK_FILE: (frozenset({"Polygon", "MultiPolygon"}), ("FMZK_ID",)),
}

# Generous window around Vienna in WGS84 (lon_min, lat_min, lon_max, lat_max). The city spans
# roughly 16.18-16.58 E, 48.11-48.33 N; anything outside this window is not "a building a bit
# further out", it is the wrong CRS or a swapped axis order (a swap lands near (48.2, 16.4)).
VIENNA_BOUNDS_WGS84 = (16.0, 47.9, 16.8, 48.5)

#: ``integrity.basis`` value written by :func:`cache_hash` — hashes pinned offline, after the
#: fact, from an already-fetched cache. :func:`fetch` writes ``fetch_time`` instead.
OFFLINE_MIGRATION_BASIS = "offline_migration"
FETCH_TIME_BASIS = "fetch_time"

_MAX_LISTED = 10  # cap per problem family so one systemic fault cannot drown the report


class CacheError(RuntimeError):
    """Raised when the cache is not fit to run the pipeline against."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _layer_bbox(features: list[dict[str, Any]]) -> list[float] | None:
    """(min_lon, min_lat, max_lon, max_lat) over every parseable geometry, or None if none."""
    from shapely.geometry import shape

    bounds: tuple[float, float, float, float] | None = None
    for feature in features:
        raw = feature.get("geometry")
        if not raw:
            continue
        try:
            b = shape(raw).bounds
        except Exception:  # noqa: BLE001 - unparseable geometry is reported by verify, not here
            continue
        bounds = (
            b
            if bounds is None
            else (
                min(bounds[0], b[0]),
                min(bounds[1], b[1]),
                max(bounds[2], b[2]),
                max(bounds[3], b[3]),
            )
        )
    return list(bounds) if bounds is not None else None


def integrity_block(cache_dir: Path, *, basis: str, note: str | None = None) -> dict[str, Any]:
    """The manifest's ``integrity`` object: a sha256 for EVERY raw input file (RTI-011).

    Vector entries also carry the per-layer feature count and bbox, so a substituted-but-
    well-formed file is caught even if someone also recomputes its hash: the counts and extent
    must still agree with what verification recomputes from the bytes on disk.
    """
    vectors: dict[str, dict[str, Any]] = {}
    for filename in (*BBOX_LAYERS, FMZK_FILE):
        path = cache_dir / filename
        if not path.is_file():
            raise CacheError(f"cannot hash {filename}: missing from {cache_dir}")
        try:
            features = json.loads(path.read_text(encoding="utf-8")).get("features", [])
        except json.JSONDecodeError as error:
            raise CacheError(f"cannot hash {filename}: does not parse as JSON ({error})") from error
        vectors[filename] = {
            "sha256": _sha256_file(path),
            "feature_count": len(features),
            "bbox_wgs84": _layer_bbox(features),
        }
    tiles = {
        path.relative_to(cache_dir).as_posix(): _sha256_file(path)
        for path in sorted(cache_dir.glob("tiles/*/*/*/*.jpeg"))
    }
    block: dict[str, Any] = {
        "hash_algorithm": "sha256",
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "basis": basis,
        "vectors": vectors,
        "tiles": tiles,
    }
    if note:
        block["note"] = note
    return block


def cache_hash(cfg: config.Config) -> dict[str, Any]:
    """Offline migration (RTI-011): pin the ALREADY-FETCHED cache content with sha256 hashes.

    The committed cache was fetched before content hashing existed, and the pipeline is
    offline — a refetch is not available. So this maintenance step computes hashes from the
    **currently committed** cache files and writes them into the manifest, together with a
    ``basis``/``note`` recording exactly what happened: hashes computed offline on the given
    date from the already-fetched cache. That pins the content **forward** — any later change
    to a cached byte fails verification — and deliberately does NOT claim the bytes were
    verified at fetch time. A future ``cache-build`` writes fetch-time hashes and makes this
    command unnecessary.

    Everything else in the manifest is left untouched: this never rewrites the recorded
    requests, counts or fetch timestamp to make a check pass.
    """
    dst = cfg.study_area.cache_dir
    manifest_path = dst / "manifest.json"
    if not manifest_path.is_file():
        raise CacheError(
            f"no manifest at {manifest_path} - nothing to migrate; a fresh cache-build writes "
            f"fetch-time hashes itself"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fetched = str(manifest.get("generated_at", "unknown"))[:10]
    today = datetime.now(timezone.utc).date().isoformat()
    note = (
        f"Content hashes computed OFFLINE on {today} from the already-fetched committed cache "
        f"(fetched {fetched}). They pin the cached content forward from {today}; they do not "
        f"certify integrity at fetch time. A future cache-build records fetch-time hashes "
        f"instead."
    )
    manifest["integrity"] = integrity_block(dst, basis=OFFLINE_MIGRATION_BASIS, note=note)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest["integrity"]


def _tile_problem(path: Path) -> str | None:
    """Fully decode a cached tile. Returns a problem description, or None if usable.

    Header inspection is not enough: a truncated download keeps a valid SOI/SOF and the
    declared 256x256, so a header-only check certifies a tile that will decode into a grey
    band across a roof. ``Image.load()`` forces the entropy-coded scan to be decoded, which
    is the only thing that actually proves the bytes are complete.
    """
    from PIL import Image

    if path.stat().st_size == 0:
        return "zero bytes"
    try:
        with Image.open(path) as image:
            image.load()
            size = image.size
    except Exception as error:  # noqa: BLE001 - any decode failure is a cache problem
        return f"does not decode ({type(error).__name__}: {error})"
    if size != (TILE_PX, TILE_PX):
        return f"{size[0]}x{size[1]}, expected {TILE_PX}x{TILE_PX}"
    return None


def _geometry_problem(raw_geometry: Any, expected_types: frozenset[str]) -> str | None:
    """Why one feature's geometry is unusable, or None.

    Policy (RTI-026): an invalid geometry FAILS verification. It is never repaired here —
    ``buffer(0)``/``make_valid`` change the shape every downstream area, IoU and containment
    figure is computed from, so a repair needs an explicit decision, not a verifier's whim.
    """
    from shapely.geometry import shape

    if not raw_geometry:
        return "empty geometry (null or absent)"
    geometry_type = raw_geometry.get("type") if isinstance(raw_geometry, dict) else None
    if geometry_type not in expected_types:
        return f"geometry type {geometry_type!r}, expected one of {sorted(expected_types)}"
    try:
        geom = shape(raw_geometry)
    except Exception as error:  # noqa: BLE001 - any unparseable geometry is a cache problem
        return f"geometry does not parse ({type(error).__name__}: {error})"
    if geom.is_empty:
        return "empty geometry (no coordinates)"
    if not geom.is_valid:
        from shapely.validation import explain_validity

        return (
            f"invalid geometry ({explain_validity(geom)}); verification fails rather than "
            f"repairing - a repair would need an explicit decision"
        )
    lon_min, lat_min, lon_max, lat_max = VIENNA_BOUNDS_WGS84
    b = geom.bounds
    if not (lon_min <= b[0] and b[2] <= lon_max and lat_min <= b[1] and b[3] <= lat_max):
        return (
            f"coordinates {tuple(round(v, 6) for v in b)} outside plausible WGS84 Vienna "
            f"bounds {VIENNA_BOUNDS_WGS84} - wrong CRS or swapped lon/lat?"
        )
    return None


def _vector_layer_problems(filename: str, features: list[dict[str, Any]]) -> list[str]:
    """RTI-026 checks for one cached layer: geometry, CRS sanity, required fields."""
    expected_types, required_fields = VECTOR_LAYER_CHECKS[filename]
    problems: list[str] = []
    for index, feature in enumerate(features):
        label = feature.get("id") or f"feature[{index}]"
        properties = feature.get("properties") or {}
        for field in required_fields:
            if properties.get(field) is None:
                problems.append(f"{filename} {label}: required field {field} missing or null")
        problem = _geometry_problem(feature.get("geometry"), expected_types)
        if problem:
            problems.append(f"{filename} {label}: {problem}")
    return problems


def _capped(problems: list[str], overflow_label: str) -> list[str]:
    if len(problems) <= _MAX_LISTED:
        return problems
    return [*problems[:_MAX_LISTED], f"... and {len(problems) - _MAX_LISTED} {overflow_label}"]


def _rounded_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    with contextlib.suppress(TypeError, ValueError):
        return [round(float(v), 9) for v in value]
    return None


def _needed_tiles(
    roof_features: list[dict[str, Any]], zoom: int, margin_m: float, crs: str
) -> set[tuple[int, int]]:
    """Tiles covering each pinned roof plus the crop margin, in projected metres."""
    from shapely.geometry import shape

    tiles: set[tuple[int, int]] = set()
    for feature in roof_features:
        bounds = shape(feature["geometry"]).bounds
        padded = geometry.buffer_bbox_m(bounds, margin_m, crs)
        col_min, row_min, col_max, row_max = wmts.tile_range(padded, zoom)
        tiles |= {
            (row, col)
            for row in range(row_min, row_max + 1)
            for col in range(col_min, col_max + 1)
        }
    return tiles


def _prune_stale_tiles(tile_root: Path, keep: set[tuple[int, int]]) -> list[str]:
    """Delete cached tiles under one layer/zoom directory that are no longer required.

    Without this, a tile written by an earlier run with different settings — or a zero-byte
    file left by a failed copy — stays in the cache forever and fails verification after an
    otherwise perfect rebuild. The rebuild would look broken when the cache was merely stale.

    Deletion is scoped to ``<cache>/tiles/<layer>/<zoom>/<row>/<col>.jpeg`` and skips any path
    that does not parse as a tile, so nothing outside the generated tile tree can be removed.
    """
    removed: list[str] = []
    if not tile_root.is_dir():
        return removed

    for path in sorted(tile_root.glob("*/*.jpeg")):
        try:
            row, col = int(path.parent.name), int(path.stem)
        except ValueError:
            continue  # not a tile this tool generated; leave it alone
        if (row, col) not in keep:
            path.unlink()
            removed.append(f"{row}/{col}")

    for directory in sorted(p for p in tile_root.iterdir() if p.is_dir()):
        if not any(directory.iterdir()):
            directory.rmdir()
    return removed


def fetch(cfg: config.Config) -> dict[str, Any]:
    """Fetch every input the offline pipeline needs. Requires network access."""
    import requests

    area = cfg.study_area
    dst = area.cache_dir
    dst.mkdir(parents=True, exist_ok=True)
    pinned = {b.objectid for b in area.buildings}

    for filename, layer in BBOX_LAYERS.items():
        payload = wfs.fetch_features(layer, area.bbox_wgs84, require_non_empty=False)
        wfs.cache_features(payload, dst / filename)
        print(f"  {filename:<28} {len(payload.get('features', [])):>6} features  [{layer}]")

    roofs = json.loads((dst / "roof_records_2025.geojson").read_text())["features"]
    present = {f["properties"]["OBJECTID"] for f in roofs}
    if not pinned <= present:
        raise CacheError(f"pinned buildings missing from the fetch: {sorted(pinned - present)}")

    buffer_m = float(cfg.threshold("join", "fmzk_buffer_m"))
    # fetch_parts_live applies join.fmzk_buffer_m itself, so it must be handed the PLAIN study
    # bbox. Passing an already-buffered bbox would buffer twice (~300 m) while the manifest
    # claimed 150 m — a fetch that is wrong in the direction that hides itself, because more
    # data than expected still produces plausible joins.
    parts_payload = units.fetch_parts_live(area.bbox_wgs84, cfg)
    wfs.cache_features(parts_payload, dst / FMZK_FILE)
    parts = parts_payload.get("features", [])
    if not parts:
        raise CacheError(
            f"FMZK fetch returned no features for the buffered bbox; without units there is "
            f"nothing to cross-check the {len(pinned)} pinned roof records against"
        )
    # Recomputed only to record what the buffered extent should be, never to re-fetch with it.
    fmzk_bbox = units.buffered_fetch_bbox(area.bbox_wgs84, cfg)
    print(f"  {FMZK_FILE:<28} {len(parts):>6} parts     [buffered {buffer_m:g} m, kept whole]")

    zoom = area.imagery_zoom
    layer = str(cfg.threshold("imagery", "layer"))
    margin_m = float(cfg.threshold("imagery", "crop_margin_m"))
    selected = [f for f in roofs if f["properties"]["OBJECTID"] in pinned]
    tiles = _needed_tiles(selected, zoom, margin_m, area.crs_metric)
    pruned = _prune_stale_tiles(dst / "tiles" / layer / str(zoom), tiles)
    if pruned:
        print(f"  pruned {len(pruned)} stale tile(s) no longer required by this config")
    with requests.Session() as session:
        for index, (row, col) in enumerate(sorted(tiles), start=1):
            wmts.fetch_tile(layer, zoom, col, row, dst / "tiles", session=session)
            if index % 25 == 0 or index == len(tiles):
                print(f"  tiles {index}/{len(tiles)}")

    manifest = {
        "study_area": area.name,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config_hash": cfg.config_hash,
        "licence": config.LICENCE,
        "attribution": config.ATTRIBUTION,
        "requests": {
            "study_bbox_wgs84": list(area.bbox_wgs84),
            "fmzk_requested_bbox_wgs84": list(fmzk_bbox),
            "fmzk_buffer_m": buffer_m,
            "fmzk_subset": "complete buffered extent, untrimmed",
            "imagery_layer": layer,
            "imagery_zoom": zoom,
            "crop_margin_m": margin_m,
            "crs_metric": area.crs_metric,
        },
        "counts": {
            "pinned_buildings": len(area.buildings),
            "fmzk_parts": len(parts),
            "tiles": len(tiles),
            "stale_tiles_pruned": len(pruned),
        },
        # RTI-011: hashed at fetch time, from the bytes as written. The offline migration path
        # (cache_hash) exists only for caches fetched before hashing did.
        "integrity": integrity_block(
            dst,
            basis=FETCH_TIME_BASIS,
            note="Content hashes computed at fetch time from the bytes as written.",
        ),
    }
    (dst / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify(cfg: config.Config, *, quiet: bool = False) -> list[str]:
    """Re-read the whole cache. Returns a list of problems; empty means fit to run.

    Checks, in order: manifest request fields against the live config; content hashes,
    feature counts and bboxes against the manifest's ``integrity`` block (RTI-011 — a manifest
    without one is itself a problem); per-feature geometry validity, expected types, Vienna
    CRS sanity and required fields (RTI-026 — invalid geometry FAILS, nothing is repaired);
    pinned-building presence and geometry; and full decode of every required tile.
    """
    area = cfg.study_area
    dst = area.cache_dir
    problems: list[str] = []

    manifest_path = dst / "manifest.json"
    manifest: dict[str, Any] = {}
    if not manifest_path.exists():
        problems.append("manifest.json missing - cache provenance is unknown")
    else:
        manifest = json.loads(manifest_path.read_text())
        requested = manifest.get("requests", {})
        expected_requests = {
            "study_bbox_wgs84": [round(v, 9) for v in area.bbox_wgs84],
            "fmzk_requested_bbox_wgs84": [
                round(v, 9) for v in units.buffered_fetch_bbox(area.bbox_wgs84, cfg)
            ],
            "fmzk_buffer_m": float(cfg.threshold("join", "fmzk_buffer_m")),
            "imagery_layer": str(cfg.threshold("imagery", "layer")),
            "imagery_zoom": area.imagery_zoom,
            "crop_margin_m": float(cfg.threshold("imagery", "crop_margin_m")),
            "crs_metric": area.crs_metric,
        }
        for field, expected in expected_requests.items():
            actual = requested.get(field)
            if field.endswith("bbox_wgs84") and isinstance(actual, list):
                with contextlib.suppress(TypeError, ValueError):
                    actual = [round(float(value), 9) for value in actual]
            if actual != expected:
                problems.append(
                    f"manifest {field}={actual!r} does not match the live config value "
                    f"{expected!r}"
                )

    # Parse every vector file once; None marks a file that is missing or does not parse.
    vectors: dict[str, list[dict[str, Any]] | None] = {}
    for filename in (*BBOX_LAYERS, FMZK_FILE):
        path = dst / filename
        if not path.exists():
            problems.append(f"missing {filename}")
            vectors[filename] = None
            continue
        try:
            features = json.loads(path.read_text()).get("features")
        except json.JSONDecodeError as error:
            problems.append(f"{filename} does not parse: {error}")
            vectors[filename] = None
            continue
        if not features:
            problems.append(f"{filename} contains no features")
        vectors[filename] = list(features or [])

    # --- content integrity against the manifest (RTI-011) ------------------------------------
    integrity = manifest.get("integrity") if manifest else None
    disk_tiles = {
        path.relative_to(dst).as_posix(): path
        for path in sorted(dst.glob("tiles/*/*/*/*.jpeg"))
    }
    if manifest and not integrity:
        problems.append(
            "manifest has no content hashes (no integrity block): content integrity cannot be "
            "checked. Run 'propx-roofs cache-hash' to pin the existing cache offline, or "
            "rebuild with cache-build."
        )
    elif integrity:
        if integrity.get("hash_algorithm") != "sha256":
            problems.append(
                f"manifest integrity uses unsupported hash_algorithm="
                f"{integrity.get('hash_algorithm')!r}, expected 'sha256'"
            )
        else:
            vector_entries = integrity.get("vectors", {})
            for filename in (*BBOX_LAYERS, FMZK_FILE):
                entry = vector_entries.get(filename)
                path = dst / filename
                if entry is None:
                    problems.append(f"manifest integrity records no hash for {filename}")
                    continue
                if not path.is_file():
                    continue  # already reported as missing above
                actual_sha = _sha256_file(path)
                if actual_sha != entry.get("sha256"):
                    problems.append(
                        f"{filename}: sha256 {actual_sha} does not match the manifest "
                        f"({entry.get('sha256')}) - content changed since it was hashed"
                    )
                features = vectors.get(filename)
                if features is None:
                    continue
                if entry.get("feature_count") != len(features):
                    problems.append(
                        f"{filename}: {len(features)} features on disk but the manifest "
                        f"records {entry.get('feature_count')}"
                    )
                if _rounded_bbox(entry.get("bbox_wgs84")) != _rounded_bbox(
                    _layer_bbox(features)
                ):
                    problems.append(
                        f"{filename}: layer bbox {_layer_bbox(features)!r} does not match "
                        f"the manifest ({entry.get('bbox_wgs84')!r})"
                    )
            tile_entries = integrity.get("tiles", {})
            tile_hash_problems: list[str] = []
            for relative, path in disk_tiles.items():
                recorded = tile_entries.get(relative)
                if recorded is None:
                    tile_hash_problems.append(
                        f"tile {relative} present on disk but not hashed in the manifest"
                    )
                elif _sha256_file(path) != recorded:
                    tile_hash_problems.append(
                        f"tile {relative}: sha256 does not match the manifest - content "
                        f"changed since it was hashed"
                    )
            for relative in tile_entries:
                if relative not in disk_tiles:
                    tile_hash_problems.append(
                        f"tile {relative} hashed in the manifest but missing from disk"
                    )
            problems += _capped(tile_hash_problems, "further tile integrity problem(s)")

    # --- per-feature geometry, CRS sanity and required fields (RTI-026) -----------------------
    for filename, features in vectors.items():
        if features is None:
            continue
        problems += _capped(
            _vector_layer_problems(filename, features),
            f"further problem(s) in {filename}",
        )

    roof_features = vectors.get("roof_records_2025.geojson") or []
    pinned = {b.objectid for b in area.buildings}
    try:
        present = {f["properties"]["OBJECTID"] for f in roof_features}
    except (KeyError, TypeError) as error:
        present = set()
        problems.append(f"roof_records_2025.geojson unusable: {error}")
    missing = pinned - present
    if roof_features and missing:
        problems.append(f"pinned buildings absent from the cache: {sorted(missing)}")

    # The pinned buildings specifically: everything downstream is computed from exactly these
    # geometries, so they are named individually rather than lost in the per-layer sweep.
    expected_roof_types, _ = VECTOR_LAYER_CHECKS["roof_records_2025.geojson"]
    selected = [
        feature
        for feature in roof_features
        if feature.get("properties", {}).get("OBJECTID") in pinned
    ]
    for feature in selected:
        problem = _geometry_problem(feature.get("geometry"), expected_roof_types)
        if problem:
            objectid = feature.get("properties", {}).get("OBJECTID")
            problems.append(f"pinned building OBJECTID {objectid}: {problem}")

    layer = str(cfg.threshold("imagery", "layer"))
    zoom = area.imagery_zoom
    margin_m = float(cfg.threshold("imagery", "crop_margin_m"))
    usable_selected = [
        feature
        for feature in selected
        if _geometry_problem(feature.get("geometry"), expected_roof_types) is None
    ]
    required_tiles = _needed_tiles(usable_selected, zoom, margin_m, area.crs_metric)
    tile_root = dst / "tiles" / layer / str(zoom)
    missing_tiles = [
        tile_root / str(row) / f"{col}.jpeg"
        for row, col in sorted(required_tiles)
        if not (tile_root / str(row) / f"{col}.jpeg").exists()
    ]
    for tile in missing_tiles:
        problems.append(f"missing required tile {tile.relative_to(dst)}")

    manifest_tile_count = manifest.get("counts", {}).get("tiles")
    if manifest and manifest_tile_count != len(required_tiles):
        problems.append(
            f"manifest tiles={manifest_tile_count!r} does not match the {len(required_tiles)} "
            "tiles required by the live config and pinned roofs"
        )

    tiles = sorted(disk_tiles.values())
    if not tiles:
        problems.append("no tiles in cache")
    bad: list[str] = []
    for tile in tiles:
        problem = _tile_problem(tile)
        if problem:
            bad.append(f"{tile.relative_to(dst)}: {problem}")
    problems += _capped(bad, "further unusable tiles")

    if not quiet:
        print(f"cache: {dst}")
        print(f"  tiles checked : {len(tiles)} ({len(bad)} unusable)")
        basis = (integrity or {}).get("basis", "NONE - no content hashes")
        print(f"  content hashes: {basis}")
        print(
            f"  fmzk buffer   : {manifest.get('requests', {}).get('fmzk_buffer_m', 'UNKNOWN')} m"
        )
        for problem in problems:
            print(f"  PROBLEM: {problem}")
        print("  OK - cache is fit to run" if not problems else f"  {len(problems)} problem(s)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--verify", action="store_true", help="verify the existing cache only (offline)"
    )
    group.add_argument(
        "--hash",
        action="store_true",
        help=(
            "offline migration: write sha256 content hashes for the EXISTING cache into its "
            "manifest (recorded as pinned-forward-from-today, not fetch-time verification), "
            "then verify"
        ),
    )
    args = parser.parse_args(argv)

    cfg = config.load()
    if args.hash:
        integrity = cache_hash(cfg)
        print(
            f"hashed {len(integrity['vectors'])} vector file(s) and "
            f"{len(integrity['tiles'])} tile(s); basis={integrity['basis']}"
        )
    elif not args.verify:
        print("fetching study-area cache (this is the only network step)")
        manifest = fetch(cfg)
        print(json.dumps(manifest["requests"], indent=2))
    return 1 if verify(cfg) else 0


if __name__ == "__main__":
    raise SystemExit(main())
