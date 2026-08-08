"""The semantic comparison tool: what counts as the same result, and what must not.

The tool defines the SEMANTIC reproducibility level (RTI-001/RTI-022): published values,
availability, flags and geometry must agree; the run's own biography — when it ran, which
commit, which interpreter, which library builds — must not count as a difference. These tests
pin both directions: real differences are reported readably and exit non-zero, and biography
alone never does.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from propx_roofs import semantic_compare
from propx_roofs.semantic_compare import EXCLUDED_RUN_FIELDS, byte_compare, compare


def _document() -> dict[str, Any]:
    """A small but structurally representative document. Not schema-complete on purpose:
    compare() must work on any two documents, including future schema versions."""
    return {
        "run": {
            "generated_at": "2026-08-03T12:00:00+00:00",
            "pipeline_version": "0.2.0",
            "schema_version": "1.1.0",
            "config_hash": "6bd97b1a627b4b40",
            "algorithm_parameters_hash": "58ab1b09ee18ffc9",
            "git": {"commit": "a" * 40, "dirty": False},
            "runtime": {"python": "3.11.3", "platform": "macOS-26.5.2-arm64-arm-64bit"},
            "dependencies": {"numpy": "2.4.6"},
        },
        "buildings": [
            {
                "building_id": "vie-swv-001",
                "authoritative_roof_polygon": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [16.37810, 48.18510],
                            [16.37850, 48.18510],
                            [16.37850, 48.18540],
                            [16.37810, 48.18510],
                        ]
                    ],
                    "crs": "EPSG:4326",
                },
                "delineation": {
                    "iou": 0.9685,
                    "hausdorff_m": 1.1,
                    "alignment_warning": {"flag": False},
                },
                "attributes": {
                    "roof_area_m2": {
                        "value": 2319.5,
                        "availability": "derived",
                        "confidence": {"score": 0.85},
                        "image_evidence": None,
                    },
                    "green_roof": {
                        "value": None,
                        "availability": "unavailable",
                        "image_evidence": {
                            "indicates": None,
                            "quality": {"shadow_fraction": 0.62, "area_m2_approx": 145.2},
                        },
                    },
                },
                "review_flags": [],
            }
        ],
    }


def test_identical_documents_are_equivalent() -> None:
    assert compare(_document(), _document()) == []


def test_the_runs_biography_is_not_a_difference() -> None:
    """generated_at, git, runtime and dependencies describe the act of running, not the
    result; two honest reproductions differ there by construction."""
    changed = _document()
    changed["run"]["generated_at"] = "2027-01-01T00:00:00+00:00"
    changed["run"]["git"] = {"commit": None, "dirty": None, "note": "installed wheel"}
    changed["run"]["runtime"] = {"python": "3.12.1", "platform": "Linux-x86_64"}
    changed["run"]["dependencies"] = {"numpy": "2.5.0"}
    assert compare(_document(), changed) == []
    assert set(EXCLUDED_RUN_FIELDS) == {"generated_at", "git", "runtime", "dependencies"}


def test_a_changed_config_hash_is_a_semantic_difference() -> None:
    changed = _document()
    changed["run"]["config_hash"] = "ffffffffffffffff"
    diffs = compare(_document(), changed)
    assert len(diffs) == 1 and "$.run.config_hash" in diffs[0]


def test_a_published_value_must_match_within_tolerance() -> None:
    changed = _document()
    changed["buildings"][0]["attributes"]["roof_area_m2"]["value"] = 2319.5 + 1e-12
    assert compare(_document(), changed) == []

    changed["buildings"][0]["attributes"]["roof_area_m2"]["value"] = 2319.6
    diffs = compare(_document(), changed)
    assert len(diffs) == 1
    assert "$.buildings[vie-swv-001].attributes.roof_area_m2.value" in diffs[0]
    assert "2319.5" in diffs[0] and "2319.6" in diffs[0]


def test_geometry_coordinates_use_the_base_tolerance() -> None:
    changed = _document()
    ring = changed["buildings"][0]["authoritative_roof_polygon"]["coordinates"][0]
    ring[0][0] += 5e-10  # inside the default 1e-9
    assert compare(_document(), changed) == []

    ring[0][0] += 1e-6  # a real coordinate shift
    diffs = compare(_document(), changed)
    assert len(diffs) == 1 and "authoritative_roof_polygon.coordinates" in diffs[0]


def test_quality_diagnostics_honour_the_looser_knob_but_published_values_do_not() -> None:
    """The escape hatch for cross-version CV drift must not widen anything published."""
    changed = _document()
    quality = changed["buildings"][0]["attributes"]["green_roof"]["image_evidence"]["quality"]
    quality["shadow_fraction"] = 0.625  # plausible OpenCV-version drift
    changed["buildings"][0]["attributes"]["roof_area_m2"]["value"] = 2320.0  # NOT acceptable

    strict = compare(_document(), changed)
    assert len(strict) == 2  # both differences reported under the default tolerance

    loose = compare(_document(), changed, diagnostics_tolerance=0.01)
    assert len(loose) == 1, loose
    assert "roof_area_m2.value" in loose[0]
    assert all("shadow_fraction" not in diff for diff in loose)


def test_delineation_metrics_are_diagnostics_too() -> None:
    changed = _document()
    changed["buildings"][0]["delineation"]["iou"] = 0.9679
    assert compare(_document(), changed) != []
    assert compare(_document(), changed, diagnostics_tolerance=0.001) == []


def test_availability_and_review_flags_are_compared_exactly() -> None:
    changed = _document()
    changed["buildings"][0]["attributes"]["roof_area_m2"]["availability"] = "observed"
    changed["buildings"][0]["review_flags"] = [{"status": "requires_visual_review"}]
    diffs = compare(_document(), changed)
    assert any("availability" in diff for diff in diffs)
    assert any("review_flags" in diff for diff in diffs)


def test_a_missing_or_extra_building_is_reported_by_id() -> None:
    fewer = _document()
    fewer["buildings"] = []
    diffs = compare(_document(), fewer)
    assert diffs == ["$.buildings[vie-swv-001]: only in first document"]

    diffs = compare(fewer, _document())
    assert diffs == ["$.buildings[vie-swv-001]: only in second document"]


def test_a_null_versus_value_difference_is_reported() -> None:
    changed = _document()
    changed["buildings"][0]["attributes"]["green_roof"]["value"] = False
    diffs = compare(_document(), changed)
    assert len(diffs) == 1 and "green_roof.value" in diffs[0]


# --- the CLI ---------------------------------------------------------------------------------


def _write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(semantic_compare._serialize(document), encoding="utf-8")
    return path


def test_cli_exits_zero_for_equivalent_documents(tmp_path: Path, capsys) -> None:
    a = _write(tmp_path / "a.json", _document())
    b = _write(tmp_path / "b.json", _document())
    assert semantic_compare.main([str(a), str(b)]) == 0
    assert "semantically equivalent" in capsys.readouterr().out


def test_cli_exits_non_zero_and_prints_a_readable_diff(tmp_path: Path, capsys) -> None:
    a = _write(tmp_path / "a.json", _document())
    changed = _document()
    changed["buildings"][0]["attributes"]["roof_area_m2"]["value"] = 999.9
    b = _write(tmp_path / "b.json", changed)

    assert semantic_compare.main([str(a), str(b)]) == 1
    err = capsys.readouterr().err
    assert "SEMANTIC DIFFERENCE" in err
    assert "$.buildings[vie-swv-001].attributes.roof_area_m2.value" in err


def test_cli_exits_two_on_unreadable_input(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    good = _write(tmp_path / "good.json", _document())
    assert semantic_compare.main([str(bad), str(good)]) == 2


# --- byte mode -------------------------------------------------------------------------------


def test_byte_mode_accepts_identity_and_a_differing_run_git(tmp_path: Path) -> None:
    """run.git records checkout state at generation time; it is the ONE masked object."""
    a = _write(tmp_path / "a.json", _document())
    b = _write(tmp_path / "b.json", _document())
    assert byte_compare(a, b) == []

    changed = _document()
    changed["run"]["git"] = {"commit": "b" * 40, "dirty": True}
    c = _write(tmp_path / "c.json", changed)
    assert byte_compare(a, c) == []
    assert semantic_compare.main(["--bytes", str(a), str(c)]) == 0


def test_byte_mode_rejects_any_other_difference_even_inside_tolerance(tmp_path: Path) -> None:
    """Byte level means byte level: a 1e-12 drift that semantic compare accepts must fail."""
    a = _write(tmp_path / "a.json", _document())
    changed = _document()
    changed["buildings"][0]["attributes"]["roof_area_m2"]["value"] = 2319.5 + 1e-12
    b = _write(tmp_path / "b.json", changed)

    assert compare(_document(), changed) == []  # semantic: fine
    problems = byte_compare(a, b)  # byte: not fine
    assert problems and any("beyond run.git" in p for p in problems)
    assert semantic_compare.main(["--bytes", str(a), str(b)]) == 1


def test_byte_mode_refuses_a_non_canonical_file(tmp_path: Path) -> None:
    """A verdict about re-formatted bytes would be meaningless, so it is refused, not guessed."""
    a = _write(tmp_path / "a.json", _document())
    b = tmp_path / "b.json"
    b.write_text(json.dumps(_document(), indent=4), encoding="utf-8")
    problems = byte_compare(a, b)
    assert problems and any("canonical serialization" in p for p in problems)


# --- self-check ------------------------------------------------------------------------------


def test_self_check_fails_a_schema_invalid_document(tmp_path: Path, capsys) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"run": {}, "buildings": []}), encoding="utf-8")
    assert semantic_compare.main(["--self-check", str(invalid)]) == 1
    assert "SCHEMA-INVALID" in capsys.readouterr().err


def test_self_check_passes_a_valid_document(tmp_path: Path, monkeypatch, capsys) -> None:
    """Wiring test: the document is handed to the packaged schema validator unchanged (the
    validator itself is exercised for real by the invalid case above and by test_schema)."""
    from propx_roofs import schema

    seen: list[Any] = []
    monkeypatch.setattr(schema, "validate", lambda document: seen.append(document))
    doc = _write(tmp_path / "doc.json", _document())
    assert semantic_compare.main(["--self-check", str(doc)]) == 0
    assert "schema-valid" in capsys.readouterr().out
    assert seen and seen[0]["buildings"][0]["building_id"] == "vie-swv-001"


def test_deepcopy_is_not_needed_documents_are_never_mutated() -> None:
    a, b = _document(), _document()
    snapshot = copy.deepcopy(a)
    compare(a, b)
    assert a == snapshot
