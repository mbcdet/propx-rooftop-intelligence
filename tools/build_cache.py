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

    python3 tools/build_cache.py            # fetch, then verify   (needs network)
    python3 tools/build_cache.py --verify   # verify only          (offline)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from propx_roofs import config, geometry, units  # noqa: E402
from propx_roofs.sources import wfs, wmts  # noqa: E402

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


class CacheError(RuntimeError):
    """Raised when the cache is not fit to run the pipeline against."""


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
    }
    (dst / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify(cfg: config.Config) -> list[str]:
    """Re-read the whole cache. Returns a list of problems; empty means fit to run."""
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
        expected_buffer = float(cfg.threshold("join", "fmzk_buffer_m"))
        if float(requested.get("fmzk_buffer_m", -1)) != expected_buffer:
            problems.append(
                f"manifest records fmzk_buffer_m={requested.get('fmzk_buffer_m')} but config "
                f"requires {expected_buffer}; the published provenance would be false"
            )
        expected_bbox = [round(v, 9) for v in units.buffered_fetch_bbox(area.bbox_wgs84, cfg)]
        actual_bbox = [round(float(v), 9) for v in requested.get("fmzk_requested_bbox_wgs84", [])]
        if actual_bbox != expected_bbox:
            problems.append(
                f"manifest fmzk bbox {actual_bbox} does not match the configured buffered "
                f"bbox {expected_bbox}"
            )

    for filename in (*BBOX_LAYERS, FMZK_FILE):
        path = dst / filename
        if not path.exists():
            problems.append(f"missing {filename}")
            continue
        try:
            features = json.loads(path.read_text()).get("features")
        except json.JSONDecodeError as error:
            problems.append(f"{filename} does not parse: {error}")
            continue
        if not features:
            problems.append(f"{filename} contains no features")

    roofs_path = dst / "roof_records_2025.geojson"
    if roofs_path.exists():
        try:
            present = {
                f["properties"]["OBJECTID"]
                for f in json.loads(roofs_path.read_text()).get("features", [])
            }
            missing = {b.objectid for b in area.buildings} - present
            if missing:
                problems.append(f"pinned buildings absent from the cache: {sorted(missing)}")
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            problems.append(f"roof_records_2025.geojson unusable: {error}")

    tiles = sorted(dst.glob("tiles/*/*/*/*.jpeg"))
    if not tiles:
        problems.append("no tiles in cache")
    bad: list[str] = []
    for tile in tiles:
        problem = _tile_problem(tile)
        if problem:
            bad.append(f"{tile.relative_to(dst)}: {problem}")
    problems += bad[:10]
    if len(bad) > 10:
        problems.append(f"... and {len(bad) - 10} further unusable tiles")

    print(f"cache: {dst}")
    print(f"  tiles checked : {len(tiles)} ({len(bad)} unusable)")
    print(f"  fmzk buffer   : {manifest.get('requests', {}).get('fmzk_buffer_m', 'UNKNOWN')} m")
    for problem in problems:
        print(f"  PROBLEM: {problem}")
    print("  OK - cache is fit to run" if not problems else f"  {len(problems)} problem(s)")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify", action="store_true", help="verify the existing cache only (offline)"
    )
    args = parser.parse_args()

    cfg = config.load()
    if not args.verify:
        print("fetching study-area cache (this is the only network step)")
        manifest = fetch(cfg)
        print(json.dumps(manifest["requests"], indent=2))
    return 1 if verify(cfg) else 0


if __name__ == "__main__":
    raise SystemExit(main())
