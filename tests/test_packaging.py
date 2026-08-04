"""The JSON Schema has to survive installation, and the only proof is an actual install.

``schema/roof_attributes.schema.json`` is read at runtime and a validation failure fails the
run, so a wheel that imports cleanly and then cannot find its schema breaks the pipeline at the
last possible moment — after all the work, at the point of writing output. A source-tree test
cannot catch that: ``Path(__file__).with_name(...)`` and ``importlib.resources`` behave
identically in a checkout and diverge only once ``[tool.setuptools.package-data]`` is wrong.

So this module really installs the project into a throwaway prefix with ``pip install --target``
and reads the schema back **through the installed package only**, in a subprocess whose
``sys.path`` cannot reach ``src/``.

If the install cannot be performed — no pip, no build backend, no network for the build
requirement — the tests **skip with the reason printed**. They never pass by asserting
something weaker instead, because a green tick that did not install anything is exactly the
false assurance this file exists to prevent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from propx_roofs import config, schema

PACKAGE = "propx_roofs"
SCHEMA_RESOURCE = "roof_attributes.schema.json"
INSTALL_TIMEOUT_S = 600


def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None):
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT_S,
        check=False,
    )


@pytest.fixture(scope="session")
def installed_prefix(tmp_path_factory) -> Path:
    """Install the project into a temporary prefix and return it.

    The sources are copied out of the repository first. Building in place would write
    ``build/`` and ``*.egg-info`` into the working tree, and on a read-only or restrictive
    checkout that failure would look like a packaging problem when it is a filesystem one.
    """
    source = tmp_path_factory.mktemp("source")
    prefix = tmp_path_factory.mktemp("prefix")
    shutil.copy(config.REPO_ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(
        config.REPO_ROOT / "src",
        source / "src",
        ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"),
    )

    result = _run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(prefix), "."],
        cwd=source,
    )
    if result.returncode != 0:
        pytest.skip(
            "could not install the project into a temporary prefix, so the packaging claim "
            "was NOT verified here. This is reported, not worked around.\n"
            f"command: pip install --no-deps --target {prefix} .\n"
            f"stdout tail:\n{result.stdout[-1500:]}\n"
            f"stderr tail:\n{result.stderr[-1500:]}"
        )
    return prefix


def _in_installed_package(prefix: Path, snippet: str, tmp_path: Path):
    """Run ``snippet`` with the installed prefix as the only source of ``propx_roofs``.

    ``-S`` is not used (site-packages still supplies numpy, shapely and OpenCV), but the cwd is
    a scratch directory and ``PYTHONPATH`` is exactly the prefix, so nothing can fall back to
    the repository's ``src/``.
    """
    result = _run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        cwd=tmp_path,
        env={"PYTHONPATH": str(prefix), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, (
        f"snippet failed in the installed package:\n{result.stdout}\n{result.stderr}"
    )
    return result


def test_the_installed_package_is_the_one_under_test(installed_prefix, tmp_path):
    result = _in_installed_package(
        installed_prefix,
        """
        import propx_roofs
        print(propx_roofs.__file__)
        """,
        tmp_path,
    )
    location = Path(result.stdout.strip())
    assert location.is_relative_to(installed_prefix)
    assert not location.is_relative_to(config.REPO_ROOT / "src")


def test_the_schema_is_readable_from_the_installed_package_via_importlib_resources(
    installed_prefix, tmp_path
):
    result = _in_installed_package(
        installed_prefix,
        f"""
        import json
        from importlib import resources
        text = (
            resources.files("{PACKAGE}.schema")
            .joinpath("{SCHEMA_RESOURCE}")
            .read_text(encoding="utf-8")
        )
        document = json.loads(text)
        print(json.dumps({{"id": document["$id"], "len": len(text)}}))
        """,
        tmp_path,
    )
    reported = json.loads(result.stdout.strip())
    on_disk = (config.REPO_ROOT / "src" / PACKAGE / "schema" / SCHEMA_RESOURCE).read_text(
        encoding="utf-8"
    )
    assert reported["id"] == schema.load_schema()["$id"]
    assert reported["len"] == len(on_disk)


def test_the_installed_schema_is_byte_identical_to_the_repository_copy(
    installed_prefix, tmp_path
):
    installed = (installed_prefix / PACKAGE / "schema" / SCHEMA_RESOURCE).read_bytes()
    repository = (config.REPO_ROOT / "src" / PACKAGE / "schema" / SCHEMA_RESOURCE).read_bytes()
    assert installed == repository


def test_the_pipelines_own_schema_loader_works_from_the_installed_package(
    installed_prefix, tmp_path
):
    """``pipeline.packaged_schema`` is the function the run actually calls before reporting
    success, so it is the one that has to work from a wheel — not just any resource read."""
    result = _in_installed_package(
        installed_prefix,
        """
        from propx_roofs import pipeline
        document = pipeline.packaged_schema()
        print(document["$id"])
        """,
        tmp_path,
    )
    assert result.stdout.strip() == schema.load_schema()["$id"]


def test_the_package_data_declaration_is_still_in_pyproject():
    """The install above proves the effect; this names the cause, so a deletion is obvious."""
    text = (config.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.setuptools.package-data]" in text
    assert 'propx_roofs = ["schema/*.json"]' in text
