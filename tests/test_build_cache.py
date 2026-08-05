"""Regression tests for the cache fetch step. Fully offline: every network call is mocked.

Both bugs pinned here were invisible at runtime, which is why they need tests rather than
care:

* **Double buffering.** ``fetch_parts_live`` applies ``join.fmzk_buffer_m`` itself. Handing
  it an already-buffered bbox fetched ~300 m while the manifest recorded 150 m. Nothing
  fails — you simply get more data than you claimed, and every join still looks plausible.
* **Nesting the FeatureCollection.** ``fetch_parts_live`` returns the payload dict, not a
  feature list. Wrapping it again produced ``features: [{"type": "FeatureCollection", ...}]``
  — one "part" with no geometry, which dissolves to zero units and would have published a
  cross-check that silently verified nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import build_cache  # noqa: E402

from propx_roofs import config, units  # noqa: E402
from propx_roofs.sources import wfs, wmts  # noqa: E402


def _feature(objectid: int, lon: float = 16.3790, lat: float = 48.1855) -> dict[str, Any]:
    ring = [
        [lon, lat],
        [lon + 0.0002, lat],
        [lon + 0.0002, lat + 0.0002],
        [lon, lat + 0.0002],
        [lon, lat],
    ]
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {"OBJECTID": objectid, "BW_GEB_ID": 5000000 + objectid},
    }


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config.Config:
    """Real config, but the cache is redirected into a temporary directory."""
    monkeypatch.setattr(config, "DEFAULT_CACHE_ROOT", tmp_path / "cache")
    return config.load()


@pytest.fixture
def fetch_calls(monkeypatch: pytest.MonkeyPatch, cfg: config.Config) -> dict[str, list]:
    """Replace every network boundary and record what it was asked for."""
    calls: dict[str, list] = {"fmzk_bbox": [], "wfs": [], "tiles": []}
    pinned = [b.objectid for b in cfg.study_area.buildings]

    def fake_fetch_features(layer: str, bbox: Any, **kwargs: Any) -> dict[str, Any]:
        calls["wfs"].append((layer, tuple(bbox)))
        features = (
            [_feature(oid) for oid in pinned]
            if layer == wfs.LAYER_ROOF_RECORD_2025
            else [_feature(1)]
        )
        return {"type": "FeatureCollection", "features": features}

    def fake_fetch_parts_live(bbox: Any, _cfg: config.Config, **kwargs: Any) -> dict[str, Any]:
        calls["fmzk_bbox"].append(tuple(round(v, 9) for v in bbox))
        # The real function returns the wfs payload dict, not a list. That is the whole point.
        return {"type": "FeatureCollection", "features": [_feature(900), _feature(901)]}

    def fake_fetch_tile(layer, zoom, col, row, cache_dir, **kwargs: Any) -> Path:
        from PIL import Image

        calls["tiles"].append((row, col))
        path = Path(cache_dir) / layer / str(zoom) / str(row) / f"{col}.jpeg"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (wmts.TILE_SIZE, wmts.TILE_SIZE), (90, 90, 90)).save(path, "JPEG")
        return path

    monkeypatch.setattr(build_cache.wfs, "fetch_features", fake_fetch_features)
    monkeypatch.setattr(build_cache.units, "fetch_parts_live", fake_fetch_parts_live)
    monkeypatch.setattr(build_cache.wmts, "fetch_tile", fake_fetch_tile)
    return calls


def test_fmzk_is_fetched_once_with_the_unbuffered_study_bbox(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    """The buffer is applied by fetch_parts_live, so build_cache must not pre-apply it."""
    build_cache.fetch(cfg)

    assert len(fetch_calls["fmzk_bbox"]) == 1, "FMZK must be fetched exactly once"
    requested = fetch_calls["fmzk_bbox"][0]
    study = tuple(round(v, 9) for v in cfg.study_area.bbox_wgs84)
    buffered = tuple(round(v, 9) for v in units.buffered_fetch_bbox(cfg.study_area.bbox_wgs84, cfg))

    assert requested == study
    assert requested != buffered, "passing the buffered bbox would buffer twice"


def test_manifest_records_the_single_configured_buffered_bbox(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    manifest = build_cache.fetch(cfg)
    requests = manifest["requests"]
    expected = [round(v, 9) for v in units.buffered_fetch_bbox(cfg.study_area.bbox_wgs84, cfg)]

    assert requests["fmzk_buffer_m"] == cfg.threshold("join", "fmzk_buffer_m")
    assert [round(float(v), 9) for v in requests["fmzk_requested_bbox_wgs84"]] == expected
    # ...and verify() agrees, so a manifest that lied about the buffer is caught.
    assert build_cache.verify(cfg) == []


def test_cached_fmzk_is_a_flat_feature_list_not_a_nested_collection(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    manifest = build_cache.fetch(cfg)
    payload = json.loads((cfg.study_area.cache_dir / build_cache.FMZK_FILE).read_text())

    assert payload["type"] == "FeatureCollection"
    features = payload["features"]
    assert [f["type"] for f in features] == ["Feature", "Feature"]
    assert all("FeatureCollection" not in json.dumps(f["type"]) for f in features)
    assert all(f.get("geometry") for f in features), "a nested collection has no geometry"
    assert manifest["counts"]["fmzk_parts"] == len(features) == 2


def test_an_empty_fmzk_fetch_fails_loudly(
    cfg: config.Config, fetch_calls: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No units means the cross-check verifies nothing, which must not pass silently."""
    monkeypatch.setattr(
        build_cache.units,
        "fetch_parts_live",
        lambda *a, **k: {"type": "FeatureCollection", "features": []},
    )
    with pytest.raises(build_cache.CacheError, match="no features"):
        build_cache.fetch(cfg)


def test_verification_rejects_a_truncated_tile_that_a_header_check_would_pass(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    """The exact failure a size-or-header check misses: valid header, incomplete scan."""
    build_cache.fetch(cfg)
    assert build_cache.verify(cfg) == []

    tile = next(iter(cfg.study_area.cache_dir.glob("tiles/*/*/*/*.jpeg")))
    data = tile.read_bytes()
    tile.write_bytes(data[: len(data) // 2])  # header intact, scan cut in half

    assert tile.stat().st_size > 0
    assert build_cache._tile_problem(tile) is not None
    assert any("does not decode" in p or "expected" in p for p in build_cache.verify(cfg))


def test_stale_tiles_are_pruned_while_required_tiles_survive(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    """A stale zero-byte tile must not fail verification after a good rebuild."""
    from PIL import Image

    build_cache.fetch(cfg)
    layer = str(cfg.threshold("imagery", "layer"))
    zoom = cfg.study_area.imagery_zoom
    tile_root = cfg.study_area.cache_dir / "tiles" / layer / str(zoom)

    required = sorted(tile_root.glob("*/*.jpeg"))
    assert required, "fixture must produce some required tiles"
    required_ids = {(int(p.parent.name), int(p.stem)) for p in required}

    # One stale zero-byte tile and one stale but valid tile, neither in the required set.
    stale_empty = tile_root / "999999" / "999999.jpeg"
    stale_empty.parent.mkdir(parents=True, exist_ok=True)
    stale_empty.write_bytes(b"")
    stale_valid = tile_root / "999998" / "999998.jpeg"
    stale_valid.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (wmts.TILE_SIZE, wmts.TILE_SIZE), (1, 2, 3)).save(stale_valid, "JPEG")

    # A file the tool did not generate must be left alone even inside the tile tree.
    bystander = tile_root / "README.txt"
    bystander.write_text("not a tile")
    outside = cfg.study_area.cache_dir / "manifest.json"

    assert any("zero bytes" in p for p in build_cache.verify(cfg)), "stale tile should fail"

    manifest = build_cache.fetch(cfg)

    assert not stale_empty.exists()
    assert not stale_valid.exists()
    assert manifest["counts"]["stale_tiles_pruned"] == 2
    surviving = {(int(p.parent.name), int(p.stem)) for p in tile_root.glob("*/*.jpeg")}
    assert surviving == required_ids
    assert bystander.read_text() == "not a tile"
    assert outside.exists()  # the manifest is rewritten, not deleted
    assert build_cache.verify(cfg) == []


def test_pruning_ignores_paths_it_did_not_generate(tmp_path: Path) -> None:
    """Scope guard: only <row>/<col>.jpeg under the given layer/zoom root is ever removed."""
    root = tmp_path / "tiles" / "lb2024" / "20"
    (root / "363684").mkdir(parents=True)
    keeper = root / "363684" / "571991.jpeg"
    keeper.write_bytes(b"x")
    (root / "notes").mkdir()
    stray = root / "notes" / "notes.jpeg"
    stray.write_bytes(b"y")

    removed = build_cache._prune_stale_tiles(root, keep={(363684, 571991)})

    assert removed == []
    assert keeper.exists()
    assert stray.exists(), "a non-numeric directory is not a tile path and must be untouched"


def test_a_corrupt_cached_tile_is_replaced_rather_than_reused(tmp_path: Path) -> None:
    """A corrupt tile must not be pinned forever by a size>0 cache hit."""
    from PIL import Image

    cache = tmp_path / "tiles"
    path = cache / "lb2024" / "20" / "363684" / "571991.jpeg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xd8not a real jpeg")
    assert path.stat().st_size > 0
    assert wmts.tile_is_valid(path) is False

    class _Response:
        status_code = 200

        def __init__(self) -> None:
            import io

            buffer = io.BytesIO()
            Image.new("RGB", (wmts.TILE_SIZE, wmts.TILE_SIZE), (10, 20, 30)).save(buffer, "JPEG")
            self.content = buffer.getvalue()

        def raise_for_status(self) -> None:
            return None

    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *_args: Any, **_kwargs: Any) -> _Response:
            self.calls += 1
            return _Response()

    session = _Session()
    result = wmts.fetch_tile("lb2024", 20, 571991, 363684, cache, session=session, delay=0)

    assert session.calls == 1, "the corrupt tile should have triggered a refetch"
    assert wmts.tile_is_valid(result)

    # A now-valid tile is served from cache without another request.
    wmts.fetch_tile("lb2024", 20, 571991, 363684, cache, session=session, delay=0)
    assert session.calls == 1
