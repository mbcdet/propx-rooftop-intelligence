"""The command line contract: what it writes, and — more importantly — when it refuses to.

The exit code is the only thing ``make`` and CI read, so the failure paths matter more than the
happy one. Three of the tests below check that a run which could not produce a trustworthy
document exits non-zero **and prints no success line**: a green build holding an invalid file is
worse than a red one, because nobody looks at it again.
"""

from __future__ import annotations

import json

import pytest

from propx_roofs import cli, config, pipeline
from propx_roofs.schema import SchemaValidationError

GENERATED_AT = "2026-08-04T00:00:00+00:00"


@pytest.fixture(scope="module")
def cfg():
    return config.load()


# ---------------------------------------------------------------------------------------
# cache-verify
# ---------------------------------------------------------------------------------------


def test_cache_verify_exits_zero_on_the_committed_cache(capsys):
    assert cli.main(["cache-verify"]) == 0
    assert "cache OK" in capsys.readouterr().out


def test_cache_verify_exits_non_zero_when_the_cache_is_bad(monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "verify_cache", lambda _cfg: ["synthetic problem"])
    assert cli.main(["cache-verify"]) == 1
    assert "cache OK" not in capsys.readouterr().out


# ---------------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cli_run(tmp_path_factory):
    """The one real CLI run the read-only tests share — executed with sockets blocked.

    Blocking the socket here rather than in a separate test means the offline guarantee is
    asserted about the *same* run that produced the outputs below, and costs no extra pass over
    the ten buildings.
    """
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("the pipeline attempted a network connection")

    out = tmp_path_factory.mktemp("cli_run")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", refuse)
        patch.setattr(socket.socket, "connect_ex", refuse)
        patch.setattr(socket, "create_connection", refuse)
        code = cli.main(["run", "--output-dir", str(out), "--generated-at", GENERATED_AT])
    return code, out


def test_run_is_offline(cli_run):
    """No socket was opened during the run above. The only fetch path is tools/build_cache.py."""
    assert cli_run[0] == 0


def test_run_exits_zero_and_writes_a_validated_document(cli_run):
    code, out = cli_run
    assert code == 0

    path = out / "roof_attributes.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    pipeline.validate_output(document)
    assert len(document["buildings"]) == 10
    assert document["run"]["generated_at"] == GENERATED_AT


def test_run_writes_one_overlay_per_building(cli_run):
    _, out = cli_run
    overlays = sorted(p.name for p in (out / "overlays").glob("*.png"))
    assert overlays == [f"vie-swv-{n:03d}.png" for n in range(1, 11)]


def test_run_exits_non_zero_and_writes_nothing_when_the_cache_fails(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(pipeline, "verify_cache", lambda _cfg: ["synthetic cache problem"])
    assert cli.main(["run", "--output-dir", str(tmp_path)]) == 1
    assert not (tmp_path / "roof_attributes.json").exists()
    assert "wrote" not in capsys.readouterr().out


def test_run_exits_non_zero_and_writes_nothing_on_a_schema_violation(
    monkeypatch, tmp_path, capsys
):
    """``run`` itself is substituted: what is under test is the CLI's response to an invalid
    document, and ``pipeline.validate_output`` already refuses to write one (test_pipeline)."""

    def reject(*args, **kwargs):
        raise SchemaValidationError("synthetic schema violation")

    monkeypatch.setattr(pipeline, "run", reject)
    assert cli.main(["run", "--output-dir", str(tmp_path)]) == 1
    assert not (tmp_path / "roof_attributes.json").exists()
    assert "wrote" not in capsys.readouterr().out


def test_validation_runs_before_anything_is_written(cfg, monkeypatch, tmp_path):
    """The ordering that makes the exit code trustworthy: no file survives a failed validation."""
    monkeypatch.setattr(pipeline, "segment_roof", lambda crop, cfg: (None, "not_under_test"))
    monkeypatch.setattr(
        pipeline, "validate_output", lambda d: (_ for _ in ()).throw(SchemaValidationError("x"))
    )
    with pytest.raises(SchemaValidationError):
        pipeline.run(cfg, generated_at=GENERATED_AT, output_dir=tmp_path)
    assert not (tmp_path / "roof_attributes.json").exists()
    assert not (tmp_path / "overlays").exists()


def test_run_exits_non_zero_when_a_pinned_record_is_missing(monkeypatch, tmp_path, capsys):
    real = pipeline.load_roof_records

    def drop_one(cfg):
        records = real(cfg)
        records.pop(cfg.study_area.buildings[0].objectid)
        return records

    monkeypatch.setattr(pipeline, "load_roof_records", drop_one)
    assert cli.main(["run", "--output-dir", str(tmp_path)]) == 1
    assert not (tmp_path / "roof_attributes.json").exists()
    assert "wrote" not in capsys.readouterr().out


def test_an_unusable_config_exits_two(tmp_path, capsys):
    bad = tmp_path / "study_area.yaml"
    bad.write_text("study_area: {}\nbuildings: []\n", encoding="utf-8")
    assert cli.main(["--study-area", str(bad), "cache-verify"]) == 2


def test_a_missing_subcommand_is_rejected():
    with pytest.raises(SystemExit):
        cli.main([])
