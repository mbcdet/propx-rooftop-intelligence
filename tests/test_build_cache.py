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

import copy
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

# The builder is a real package module now (RTI-002); the historical tools/build_cache.py is a
# thin shim over it, checked as such at the bottom of this file.
from propx_roofs import cache_build as build_cache
from propx_roofs import config, units
from propx_roofs.sources import wfs, wmts

REPO_ROOT = Path(__file__).resolve().parents[1]


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
        "properties": {
            "OBJECTID": objectid,
            "BW_GEB_ID": 5000000 + objectid,
            "FMZK_ID": 4000000000 + objectid,
        },
    }


def _point_feature(objectid: int, lon: float = 16.3790, lat: float = 48.1855) -> dict[str, Any]:
    """Building-info is a point layer on the live service, and verify() checks for that."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"OBJECTID": objectid},
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
        if layer == wfs.LAYER_ROOF_RECORD_2025:
            features = [_feature(oid) for oid in pinned]
        elif layer == wfs.LAYER_BUILDING_INFO:
            features = [_point_feature(1)]
        else:
            features = [_feature(1)]
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


def test_verification_rejects_a_missing_required_tile(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    """Every tile implied by the pinned roofs and live config must be present."""
    build_cache.fetch(cfg)
    assert build_cache.verify(cfg) == []

    tile = sorted(cfg.study_area.cache_dir.glob("tiles/*/*/*/*.jpeg"))[0]
    relative = str(tile.relative_to(cfg.study_area.cache_dir))
    tile.unlink()

    problems = build_cache.verify(cfg)
    assert any("missing required tile" in problem and relative in problem for problem in problems)


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("study_bbox_wgs84", [16.0, 48.0, 16.1, 48.1]),
        ("fmzk_requested_bbox_wgs84", [16.0, 48.0, 16.1, 48.1]),
        ("fmzk_buffer_m", 200.0),
        ("imagery_layer", "wrong-layer"),
        ("imagery_zoom", 19),
        ("crop_margin_m", 99.0),
        ("crs_metric", "EPSG:3857"),
    ],
)
def test_verification_rejects_manifest_values_from_a_different_config(
    cfg: config.Config,
    fetch_calls: dict[str, list],
    field: str,
    wrong: object,
) -> None:
    """Each request field that determines cache contents is verified by name."""
    build_cache.fetch(cfg)
    path = cfg.study_area.cache_dir / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["requests"][field] = wrong
    path.write_text(json.dumps(manifest))

    assert any(field in problem for problem in build_cache.verify(cfg))


def test_unrelated_config_edit_does_not_invalidate_the_cache(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    """Output-only settings do not change which source records or tiles were cached."""
    build_cache.fetch(cfg)

    thresholds = copy.deepcopy(cfg.thresholds)
    thresholds["confidence"]["penalty"]["source_recency"] = 0.91
    edited_cfg = replace(
        cfg,
        thresholds=thresholds,
        config_hash="changed-because-an-output-only-confidence-penalty-changed",
    )

    assert build_cache.verify(edited_cfg) == []


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


# --- RTI-011: content integrity ---------------------------------------------------------------


def _tamper_vector(cfg: config.Config, filename: str, mutate) -> None:
    """Edit one cached vector file, then re-pin the hashes so only semantic checks can fire."""
    path = cfg.study_area.cache_dir / filename
    payload = json.loads(path.read_text())
    mutate(payload)
    path.write_text(json.dumps(payload))
    build_cache.cache_hash(cfg)


def test_fetch_writes_content_hashes_for_every_file(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    """A fresh build hashes at fetch time: every vector file and every tile, plus counts/bbox."""
    manifest = build_cache.fetch(cfg)
    integrity = manifest["integrity"]

    assert integrity["hash_algorithm"] == "sha256"
    assert integrity["basis"] == build_cache.FETCH_TIME_BASIS
    assert set(integrity["vectors"]) == {*build_cache.BBOX_LAYERS, build_cache.FMZK_FILE}
    for entry in integrity["vectors"].values():
        assert len(entry["sha256"]) == 64
        assert entry["feature_count"] >= 1
        assert len(entry["bbox_wgs84"]) == 4
    tiles_on_disk = sorted(cfg.study_area.cache_dir.glob("tiles/*/*/*/*.jpeg"))
    assert len(integrity["tiles"]) == len(tiles_on_disk) == manifest["counts"]["tiles"]
    assert all(len(sha) == 64 for sha in integrity["tiles"].values())
    assert build_cache.verify(cfg) == []


def test_verification_rejects_a_modified_vector_file(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    """Any changed byte in a cached GeoJSON is a hash mismatch, and a FAIL."""
    build_cache.fetch(cfg)
    path = cfg.study_area.cache_dir / build_cache.FMZK_FILE
    payload = json.loads(path.read_text())
    payload["features"][0]["properties"]["FMZK_ID"] = 999999999
    path.write_text(json.dumps(payload))

    problems = build_cache.verify(cfg)
    assert any(
        build_cache.FMZK_FILE in p and "sha256" in p and "does not match" in p for p in problems
    )


def test_verification_rejects_a_substituted_tile_that_still_decodes(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    """A well-formed but wrong tile passes the decode check; only the hash can catch it."""
    from PIL import Image

    build_cache.fetch(cfg)
    tile = next(iter(cfg.study_area.cache_dir.glob("tiles/*/*/*/*.jpeg")))
    Image.new("RGB", (wmts.TILE_SIZE, wmts.TILE_SIZE), (200, 10, 10)).save(tile, "JPEG")
    assert build_cache._tile_problem(tile) is None, "the substitute must decode cleanly"

    problems = build_cache.verify(cfg)
    assert any("sha256 does not match" in p and str(tile.name) in p for p in problems)


def test_a_manifest_without_hashes_fails_and_cache_hash_migrates_it(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    """The offline migration path: a legacy manifest fails, cache-hash pins it honestly."""
    build_cache.fetch(cfg)
    path = cfg.study_area.cache_dir / "manifest.json"
    manifest = json.loads(path.read_text())
    generated_at = manifest["generated_at"]
    del manifest["integrity"]  # simulate a manifest written before RTI-011
    path.write_text(json.dumps(manifest, indent=2))

    problems = build_cache.verify(cfg)
    assert any("no content hashes" in p for p in problems)

    integrity = build_cache.cache_hash(cfg)
    assert integrity["basis"] == build_cache.OFFLINE_MIGRATION_BASIS
    # The note is the honest part: computed offline, pinned forward, NOT fetch-time proof.
    assert "OFFLINE" in integrity["note"]
    assert "do not certify integrity at fetch time" in integrity["note"]
    assert generated_at[:10] in integrity["note"], "must name the original fetch date"

    migrated = json.loads(path.read_text())
    assert migrated["generated_at"] == generated_at, "migration must not rewrite provenance"
    assert migrated["requests"] == manifest["requests"]
    assert build_cache.verify(cfg) == []


def test_cache_hash_refuses_a_cache_with_no_manifest(cfg: config.Config) -> None:
    with pytest.raises(build_cache.CacheError, match="no manifest"):
        build_cache.cache_hash(cfg)


# --- RTI-026: geometry validation --------------------------------------------------------------


def test_verification_rejects_a_self_intersecting_ring_without_repairing_it(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    """Invalid geometry FAILS verification; nothing repairs it, not even cache-hash."""
    build_cache.fetch(cfg)
    pinned = cfg.study_area.buildings[0].objectid
    lon, lat, d = 16.3790, 48.1855, 0.0002

    def bowtie(payload: dict[str, Any]) -> None:
        for feature in payload["features"]:
            if feature["properties"]["OBJECTID"] == pinned:
                feature["geometry"]["coordinates"] = [
                    [
                        [lon, lat],
                        [lon + d, lat + d],
                        [lon + d, lat],
                        [lon, lat + d],
                        [lon, lat],
                    ]
                ]

    _tamper_vector(cfg, "roof_records_2025.geojson", bowtie)

    problems = build_cache.verify(cfg)
    assert any("invalid geometry" in p and "Self-intersection" in p for p in problems)
    # The pinned building is named specifically, not lost in the per-layer sweep.
    assert any(f"pinned building OBJECTID {pinned}" in p for p in problems)
    # And the file on disk still holds the bowtie: verification reported, it did not repair.
    kept = json.loads((cfg.study_area.cache_dir / "roof_records_2025.geojson").read_text())
    ring = next(
        f for f in kept["features"] if f["properties"]["OBJECTID"] == pinned
    )["geometry"]["coordinates"][0]
    assert ring[1] == [lon + d, lat + d], "the invalid ring must be untouched"


def test_verification_rejects_an_empty_geometry(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    build_cache.fetch(cfg)

    def drop_geometry(payload: dict[str, Any]) -> None:
        payload["features"][0]["geometry"] = None

    _tamper_vector(cfg, build_cache.FMZK_FILE, drop_geometry)
    assert any("empty geometry" in p for p in build_cache.verify(cfg))


def test_verification_rejects_a_missing_required_field(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    build_cache.fetch(cfg)

    def drop_objectid(payload: dict[str, Any]) -> None:
        del payload["features"][0]["properties"]["OBJECTID"]

    _tamper_vector(cfg, "typology.geojson", drop_objectid)
    problems = build_cache.verify(cfg)
    assert any("required field OBJECTID missing or null" in p for p in problems)


def test_verification_rejects_swapped_lon_lat_coordinates(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    """A lat,lon swap lands near (48.2, 16.4) - far outside the Vienna window, never silent."""
    build_cache.fetch(cfg)

    def swap_axes(payload: dict[str, Any]) -> None:
        ring = payload["features"][0]["geometry"]["coordinates"][0]
        payload["features"][0]["geometry"]["coordinates"] = [
            [[lat, lon] for lon, lat in ring]
        ]

    _tamper_vector(cfg, "typology.geojson", swap_axes)
    problems = build_cache.verify(cfg)
    assert any("outside plausible WGS84 Vienna bounds" in p for p in problems)


def test_verification_rejects_a_wrong_geometry_type_for_the_layer(
    cfg: config.Config, fetch_calls: dict[str, list]
) -> None:
    """building_info is a point layer; a polygon there means the wrong layer was cached."""
    build_cache.fetch(cfg)

    def to_polygon(payload: dict[str, Any]) -> None:
        payload["features"][0] = _feature(1)

    _tamper_vector(cfg, "building_info.geojson", to_polygon)
    problems = build_cache.verify(cfg)
    assert any("geometry type 'Polygon', expected one of ['Point']" in p for p in problems)


def test_the_tools_shim_delegates_to_the_package_module() -> None:
    """tools/build_cache.py must stay a shim: same fetch/verify objects, no second copy.

    A drifted duplicate is exactly the failure mode moving the builder into the package was
    meant to end - two verifiers disagreeing about what "fit to run" means.
    """
    shim_path = REPO_ROOT / "tools" / "build_cache.py"
    spec = importlib.util.spec_from_file_location("tools_build_cache_shim", shim_path)
    assert spec is not None and spec.loader is not None
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)

    assert shim.fetch is build_cache.fetch
    assert shim.verify is build_cache.verify
    assert shim.main is build_cache.main
    assert shim.cache_hash is build_cache.cache_hash
    assert shim.CacheError is build_cache.CacheError
