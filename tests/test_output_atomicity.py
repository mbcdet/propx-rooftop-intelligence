"""Atomic output writing (RTI-015): the final directory is never a mixed state.

These tests drive :func:`propx_roofs.pipeline.write_outputs` directly with small synthetic
documents and 4x4 PNGs, because the property under test is filesystem behaviour, not pipeline
content: a failed write must leave the previous run's outputs byte-identical, a new run must
leave no stale artifact behind, and entries the run does not own (``outputs/_solar_eval/``)
must survive every swap.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from propx_roofs import pipeline

GENERATED_AT = "2026-08-04T00:00:00+00:00"


def _image(shade: int) -> np.ndarray:
    return np.full((4, 4, 3), shade, dtype=np.uint8)


def _document(building_ids: list[str], *, overlays: bool, marker: str = "a") -> dict:
    return {
        "run": {"generated_at": GENERATED_AT, "config_hash": "cafe0123deadbeef"},
        "marker": marker,
        "buildings": [
            {
                "building_id": bid,
                "overlay": f"overlays/{bid}.png" if overlays else None,
            }
            for bid in building_ids
        ],
    }


def _write(out: Path, building_ids: list[str], *, overlays: bool = True, marker: str = "a",
           shade: int = 100) -> Path:
    images = {bid: _image(shade) for bid in building_ids} if overlays else {}
    return pipeline.write_outputs(_document(building_ids, overlays=overlays, marker=marker),
                                  images, out)


def _snapshot(out: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(out)): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()
    }


def test_a_run_writes_json_overlays_and_a_verifying_manifest(tmp_path):
    out = tmp_path / "outputs"
    path = _write(out, ["vie-swv-001", "vie-swv-002"])

    assert path == out / "roof_attributes.json"
    manifest = json.loads((out / "artifact_manifest.json").read_text(encoding="utf-8"))
    assert manifest["generated_at"] == GENERATED_AT
    assert manifest["config_hash"] == "cafe0123deadbeef"
    assert sorted(manifest["files"]) == [
        "overlays/vie-swv-001.png",
        "overlays/vie-swv-002.png",
        "roof_attributes.json",
    ]
    # The hashes are real fingerprints of the shipped bytes, not labels.
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    # No staging or trash residue anywhere next to the output directory.
    assert not list(tmp_path.glob("outputs.tmp-*")) and not list(tmp_path.glob("outputs.old-*"))


def test_the_manifest_is_byte_reproducible_under_a_pinned_generated_at(tmp_path):
    _write(tmp_path / "a", ["vie-swv-001"])
    _write(tmp_path / "b", ["vie-swv-001"])
    read = lambda d: (tmp_path / d / "artifact_manifest.json").read_bytes()  # noqa: E731
    assert read("a") == read("b")


def test_unrelated_entries_in_the_output_directory_survive_the_swap(tmp_path):
    """The swap touches only the run's own artifact names (RUN_ARTIFACT_NAMES)."""
    out = tmp_path / "outputs"
    keep = out / "_solar_eval" / "keepme"
    keep.parent.mkdir(parents=True)
    keep.write_text("do not touch", encoding="utf-8")
    (out / "NOTES.md").write_text("also unrelated", encoding="utf-8")

    _write(out, ["vie-swv-001"])
    _write(out, ["vie-swv-001"], marker="b", shade=50)

    assert keep.read_text(encoding="utf-8") == "do not touch"
    assert (out / "NOTES.md").read_text(encoding="utf-8") == "also unrelated"


def test_an_overlay_write_failure_leaves_the_previous_state_untouched(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    _write(out, ["vie-swv-001"], marker="first")
    before = _snapshot(out)

    monkeypatch.setattr(pipeline.cv2, "imwrite", lambda *a, **k: False)
    with pytest.raises(pipeline.PipelineError, match="failed to write overlay"):
        _write(out, ["vie-swv-001"], marker="second", shade=7)

    # Byte-identical previous state, and no partial staging directory left behind.
    assert _snapshot(out) == before
    assert json.loads((out / "roof_attributes.json").read_text())["marker"] == "first"
    assert not list(tmp_path.glob("outputs.tmp-*")) and not list(tmp_path.glob("outputs.old-*"))


def test_a_document_naming_a_missing_overlay_refuses_to_publish(tmp_path):
    """The staged run is verified complete against the document before anything moves."""
    out = tmp_path / "outputs"
    _write(out, ["vie-swv-001"], marker="first")
    before = _snapshot(out)

    document = _document(["vie-swv-001", "vie-swv-002"], overlays=True, marker="second")
    with pytest.raises(pipeline.PipelineError, match="incomplete"):
        pipeline.write_outputs(document, {"vie-swv-001": _image(9)}, out)

    assert _snapshot(out) == before
    assert not list(tmp_path.glob("outputs.tmp-*"))


def test_a_second_run_with_fewer_overlays_leaves_no_stale_png(tmp_path):
    out = tmp_path / "outputs"
    _write(out, ["vie-swv-001", "vie-swv-002"], marker="first")
    _write(out, ["vie-swv-001"], marker="second", shade=30)

    assert sorted(p.name for p in (out / "overlays").iterdir()) == ["vie-swv-001.png"]
    manifest = json.loads((out / "artifact_manifest.json").read_text(encoding="utf-8"))
    assert sorted(manifest["files"]) == ["overlays/vie-swv-001.png", "roof_attributes.json"]


def test_a_no_overlays_run_removes_a_previous_overlays_directory(tmp_path):
    out = tmp_path / "outputs"
    _write(out, ["vie-swv-001"], marker="first")
    assert (out / "overlays").is_dir()

    _write(out, ["vie-swv-001"], overlays=False, marker="second")

    assert not (out / "overlays").exists()
    assert json.loads((out / "roof_attributes.json").read_text())["marker"] == "second"
    manifest = json.loads((out / "artifact_manifest.json").read_text(encoding="utf-8"))
    assert list(manifest["files"]) == ["roof_attributes.json"]
    assert not list(tmp_path.glob("outputs.old-*"))
