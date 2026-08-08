"""The command line contract: what it writes, and — more importantly — when it refuses to.

The exit code is the only thing ``make`` and CI read, so the failure paths matter more than the
happy one. Three of the tests below check that a run which could not produce a trustworthy
document exits non-zero **and prints no success line**: a green build holding an invalid file is
worse than a red one, because nobody looks at it again.
"""

from __future__ import annotations

import json
import shutil

import pytest

from propx_roofs import cache_build, cli, config, pipeline
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


def test_cache_verify_accepts_an_explicit_cache_root(capsys):
    """--cache-root is the installed-wheel escape hatch; the checkout's own cache through it
    must behave exactly like the default resolution."""
    assert cli.main(["--cache-root", str(config.REPO_ROOT / "data" / "cache"), "cache-verify"]) == 0
    assert "cache OK" in capsys.readouterr().out


def test_cache_verify_refuses_a_missing_cache_root_with_guidance(tmp_path, capsys, caplog):
    """No cache at the given root is a usage error that must name the escape hatches."""
    assert cli.main(["--cache-root", str(tmp_path / "nowhere"), "cache-verify"]) == 2
    assert "cache OK" not in capsys.readouterr().out
    assert "--cache-root" in caplog.text
    assert config.DATA_ROOT_ENV in caplog.text


# ---------------------------------------------------------------------------------------
# cache-build (THE ONE NETWORKED COMMAND - fully mocked here)
# ---------------------------------------------------------------------------------------


def test_cache_build_fetches_then_verifies_and_reports_ok(monkeypatch, capsys):
    calls = []
    manifest = {"counts": {"tiles": 3}}
    monkeypatch.setattr(cache_build, "fetch", lambda cfg: calls.append("fetch") or manifest)
    monkeypatch.setattr(cache_build, "verify", lambda cfg: calls.append("verify") or [])

    assert cli.main(["cache-build"]) == 0
    out = capsys.readouterr().out
    assert calls == ["fetch", "verify"], "the freshly written cache must be re-verified"
    assert "network" in out
    assert "cache OK" in out


def test_cache_build_exits_non_zero_when_the_fetch_fails(monkeypatch, capsys):
    def refuse(cfg):
        raise cache_build.CacheError("synthetic fetch failure")

    monkeypatch.setattr(cache_build, "fetch", refuse)
    assert cli.main(["cache-build"]) == 1
    assert "cache OK" not in capsys.readouterr().out


def test_cache_build_exits_non_zero_when_the_fresh_cache_does_not_verify(monkeypatch, capsys):
    monkeypatch.setattr(cache_build, "fetch", lambda cfg: {"counts": {"tiles": 0}})
    monkeypatch.setattr(cache_build, "verify", lambda cfg: ["synthetic problem"])
    assert cli.main(["cache-build"]) == 1
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
    """No socket was opened during the run above. The one fetch path is propx-roofs cache-build."""
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


def test_review_checklist_and_record_review_close_the_strict_loop(
    cli_run, tmp_path, capsys
):
    """The whole RTI-016 closure through the CLI, against a temporary annotations COPY only:
    checklist written unchecked -> strict release-check exits 3 (awaiting) -> record-review
    warns that it asserts a human review and records the hashes -> strict exits 0. The real
    validation/audit_annotations.json is never written: that record belongs to a human."""
    code, out = cli_run
    assert code == 0
    annotations = tmp_path / "annotations_copy.json"
    shutil.copy(config.REPO_ROOT / "validation" / "audit_annotations.json", annotations)

    checklist = tmp_path / "checklist.md"
    assert (
        cli.main(["review-checklist", "--output-dir", str(out), "--out", str(checklist)])
        == 0
    )
    text = checklist.read_text(encoding="utf-8")
    assert text.count("- [ ]") == 10 and "[x]" not in text

    strict = [
        "release-check",
        "--output-dir",
        str(out),
        "--audit-annotations",
        str(annotations),
        "--require-current-audit",
    ]
    assert cli.main(strict) == 3  # consistent but awaiting review: not 0, not 1
    captured = capsys.readouterr()
    assert "AWAITING" in captured.err

    assert (
        cli.main(
            [
                "record-review",
                "--reviewer",
                "Synthetic Test Reviewer",
                "--date",
                "2026-08-08",
                "--output-dir",
                str(out),
                "--annotations",
                str(annotations),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "ASSERTS" in captured.out  # the command says out loud what running it means
    assert "reaudit_of_artifact_sha256" in captured.out

    assert cli.main(strict) == 0
    assert "audit_binding: current" in capsys.readouterr().out


def test_an_unusable_config_exits_two(tmp_path, capsys):
    bad = tmp_path / "study_area.yaml"
    bad.write_text("study_area: {}\nbuildings: []\n", encoding="utf-8")
    assert cli.main(["--study-area", str(bad), "cache-verify"]) == 2


def test_a_missing_subcommand_is_rejected():
    with pytest.raises(SystemExit):
        cli.main([])
