"""The JSON Schema has to survive installation, and the only proof is an actual install.

``schema/roof_attributes.schema.json`` is read at runtime and a validation failure fails the
run, so a wheel that imports cleanly and then cannot find its schema breaks the pipeline at the
last possible moment — after all the work, at the point of writing output. A source-tree test
cannot catch that: ``Path(__file__).with_name(...)`` and ``importlib.resources`` behave
identically in a checkout and diverge only once ``[tool.setuptools.package-data]`` is wrong.

So this module really installs the project into a throwaway prefix and reads the schema back
**through the installed package only**, in a subprocess whose ``sys.path`` cannot reach
``src/``.

Every install and build here runs **offline by design**: ``--no-build-isolation`` (the build
uses this environment's own backend instead of an isolated env that would fetch setuptools from
an index) plus ``--no-index`` (pip may not contact any index even by accident). What makes that
safe is a module-level **preflight**: before any build, ``_local_build_backend_problem`` checks
that this environment actually provides the backend ``[build-system]`` declares — setuptools
satisfying the ``>=68`` floor, and wheel. Both the locked environment (requirements.lock pins
pip/setuptools/wheel exactly) and any ``make install``-ed dev environment (the dev extra
supplies setuptools/wheel) pass it, so in those environments the tests must PASS — any
install/build error is a hard failure, never a skip.

The floor matters because of a real incident. An earlier revision used ``--no-build-isolation``
*without* a preflight, and the build silently fell back to the ambient setuptools — a
pre-PEP-621 version (59.6.0 here) that cannot read ``[project]`` from ``pyproject.toml``. It
produced an empty ``UNKNOWN-0.0.0`` wheel, pip reported ``Successfully installed``, and the
real failure surfaced three tests later as a missing schema file. Two defences remain: the
preflight (the floor is enforced before building), and artefact inspection (the fixture checks
the **artefact**, not the return code, and a dedicated regression test asserts the built
wheel's dist-info name/version and a nonempty ``propx_roofs/`` inside it).

Skipping is permitted in exactly one case: the preflight fails AND the environment is not the
locked one (a bare interpreter without setuptools>=68/wheel — i.e. neither ``make install`` nor
``make install-locked`` was run). The skip names the missing tool and both fix commands. When
the environment *claims* to be locked — importlib.metadata finds pip/setuptools/wheel at
exactly the versions pinned in requirements.lock — a preflight failure is a hard FAIL, because
then the lock itself is broken. A green tick that did not install anything is exactly the false
assurance this file exists to prevent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import textwrap
import zipfile
from importlib import metadata
from pathlib import Path

import pytest

from propx_roofs import config, schema

PACKAGE = "propx_roofs"
SCHEMA_RESOURCE = "roof_attributes.schema.json"
# A non-isolated build of this project is seconds of work. Ten minutes was long enough to hide
# a deadlock that had nothing to do with the build, so fail fast instead.
INSTALL_TIMEOUT_S = 120
# The three distributions whose exact pins in requirements.lock define "the locked environment".
TOOLCHAIN = ("pip", "setuptools", "wheel")
# The setuptools floor declared in [build-system]. Asserted against pyproject.toml below so the
# two cannot drift apart silently.
SETUPTOOLS_FLOOR = (68,)


def _installed_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def _version_at_least(version: str, floor: tuple[int, ...]) -> bool:
    try:
        from packaging.version import Version

        return Version(version) >= Version(".".join(str(part) for part in floor))
    except ImportError:  # pragma: no cover - packaging is a runtime dep here, but be safe
        released = re.match(r"(\d+(?:\.\d+)*)", version)
        if not released:
            return False
        parsed = tuple(int(part) for part in released.group(1).split("."))
        return parsed >= floor


def _lock_toolchain_pins() -> dict[str, str]:
    """The exact pip/setuptools/wheel versions pinned in requirements.lock (may be empty if the
    lock stops pinning them — which its own test below turns into a failure)."""
    pins: dict[str, str] = {}
    for line in (config.REPO_ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        matched = re.match(r"^(pip|setuptools|wheel)==(\S+)\s*$", line.strip())
        if matched:
            pins[matched.group(1)] = matched.group(2)
    return pins


def _environment_matches_lock() -> bool:
    """True when importlib.metadata finds all three toolchain distributions at EXACTLY the
    versions requirements.lock pins — the definition of 'the locked environment' here."""
    pins = _lock_toolchain_pins()
    return set(pins) == set(TOOLCHAIN) and all(
        _installed_version(name) == pinned for name, pinned in pins.items()
    )


def _local_build_backend_problem() -> str | None:
    """Preflight: does THIS environment provide the build backend [build-system] declares?

    Returns None when setuptools satisfies the >=68 floor and wheel is present — the condition
    under which --no-build-isolation is guaranteed not to reintroduce the UNKNOWN-0.0.0 failure
    mode. Otherwise returns a description of what is missing.
    """
    problems = []
    setuptools_version = _installed_version("setuptools")
    if setuptools_version is None:
        problems.append("setuptools is not installed")
    elif not _version_at_least(setuptools_version, SETUPTOOLS_FLOOR):
        floor = ".".join(str(part) for part in SETUPTOOLS_FLOOR)
        problems.append(
            f"setuptools {setuptools_version} is older than the >={floor} floor in "
            f"[build-system] (pre-PEP-621 setuptools builds an empty UNKNOWN-0.0.0 wheel)"
        )
    if _installed_version("wheel") is None:
        problems.append("wheel is not installed")
    return "; ".join(problems) or None


def _require_local_build_backend() -> None:
    """Gate for every fixture that builds: pass silently, fail hard, or (narrowly) skip.

    Skip is allowed ONLY when the backend is missing AND the environment is not the locked one.
    In the locked environment a missing backend means requirements.lock itself is broken, and
    that must fail, not skip.
    """
    problem = _local_build_backend_problem()
    if problem is None:
        return
    if _environment_matches_lock():
        pytest.fail(
            "this environment matches the requirements.lock toolchain pins exactly, yet the "
            f"build-backend preflight failed: {problem}. The lock is supposed to guarantee a "
            "working local backend; fix requirements.lock (or the environment built from it), "
            "do not skip."
        )
    pytest.skip(
        f"local build backend unavailable: {problem}. The packaging claim was NOT verified "
        "here. Fix with `make install` (unlocked dev env; the dev extra supplies "
        "setuptools/wheel) or `make install-locked` (the pinned environment)."
    )


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


def _copy_sources(destination: Path) -> None:
    """Copy the buildable sources out of the repository.

    Building in place would write ``build/`` and ``*.egg-info`` into the working tree, and on a
    read-only or restrictive checkout that failure would look like a packaging problem when it
    is a filesystem one.
    """
    shutil.copy(config.REPO_ROOT / "pyproject.toml", destination / "pyproject.toml")
    shutil.copytree(
        config.REPO_ROOT / "src",
        destination / "src",
        ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"),
    )


@pytest.fixture(scope="session")
def installed_prefix(tmp_path_factory) -> Path:
    """Install the project into a temporary prefix and return it.

    ``--no-build-isolation`` uses this environment's backend (the preflight has just proven it
    satisfies [build-system]) and ``--no-index`` forbids pip from touching any index, so the
    install is offline by construction. ``--no-deps`` keeps the runtime dependencies out — the
    prefix holds this project and nothing else, which is exactly what the import isolation
    below relies on. With the preflight green, any failure here is a FAILURE, never a skip.
    """
    _require_local_build_backend()
    source = tmp_path_factory.mktemp("source")
    prefix = tmp_path_factory.mktemp("prefix")
    _copy_sources(source)

    argv = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-build-isolation",
        "--no-index",
        "--no-deps",
        "--disable-pip-version-check",
        "--target",
        str(prefix),
        ".",
    ]
    result = _run(argv, cwd=source)
    command = " ".join(["pip"] + argv[3:])
    if result.returncode != 0:
        pytest.fail(
            "installing the project into a temporary prefix is a purely local, offline "
            "operation (--no-build-isolation --no-index --no-deps) and the preflight proved "
            "the local backend satisfies [build-system] — so this failure is real, not an "
            "environment limitation.\n"
            f"command: {command}\n"
            f"stdout tail:\n{result.stdout[-1500:]}\n"
            f"stderr tail:\n{result.stderr[-1500:]}"
        )

    # A zero exit code is NOT proof that the project was installed. Without build isolation the
    # build uses whatever setuptools is importable; a pre-PEP-621 one cannot read [project]
    # from pyproject.toml, so it cheerfully builds an empty `UNKNOWN-0.0.0` wheel containing
    # only dist-info, and pip reports "Successfully installed". Observed here with setuptools
    # 59.6.0: the prefix held 7 metadata files and no package, and the failure only surfaced
    # three tests later as a confusing missing-schema error. The preflight above should make
    # this unreachable — but check the artefact, not the return code, all the same.
    if not (prefix / PACKAGE).is_dir():
        installed = sorted(p.name for p in prefix.iterdir()) if prefix.is_dir() else []
        pytest.fail(
            f"pip exited 0 but installed no {PACKAGE!r} package, so nothing was verified.\n"
            f"This is a build failure reported as success, not a missing optional feature.\n"
            f"command: {command}\n"
            f"build backend available locally: setuptools {_installed_version('setuptools')}\n"
            f"[build-system] requires setuptools>=68; if the version above is older it cannot "
            f"read [project] from pyproject.toml and produces an empty UNKNOWN-0.0.0 wheel.\n"
            f"prefix contents: {installed}\n"
            f"stdout tail:\n{result.stdout[-1500:]}"
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


def test_the_data_root_is_resolved_by_content_not_by_position(tmp_path, monkeypatch):
    """RTI-002: an installed package must not infer the project root from its own location.

    ``Path(__file__).parents[2]`` is correct inside a checkout and meaningless in site-packages,
    where it pointed at whatever directory sat above the install. Running the installed CLI from
    ``/tmp`` therefore looked for ``/tmp/configs/study_area.yaml``. The resolver must accept a
    candidate only when the config files are actually there.
    """
    empty = tmp_path / "not-a-checkout"
    empty.mkdir()
    monkeypatch.delenv(config.DATA_ROOT_ENV, raising=False)
    monkeypatch.chdir(empty)
    assert not config._looks_like_a_checkout(empty)

    # The real checkout is still recognised, by content rather than by arithmetic.
    checkout = config.REPO_ROOT
    assert config._looks_like_a_checkout(checkout)

    # An explicit data root wins over both, so a deployment can place the files anywhere.
    staged = tmp_path / "staged"
    (staged / "configs").mkdir(parents=True)
    for name in ("study_area.yaml", "pipeline.yaml"):
        shutil.copy(checkout / "configs" / name, staged / "configs" / name)
    monkeypatch.setenv(config.DATA_ROOT_ENV, str(staged))
    assert config._resolve_data_root() == staged.resolve()


def test_a_missing_config_names_the_resolved_root(tmp_path):
    """The failure must say where it looked, not just that something was absent."""
    with pytest.raises(FileNotFoundError) as caught:
        config.load(tmp_path / "absent.yaml", tmp_path / "also-absent.yaml")
    message = str(caught.value)
    assert "study area config not found" in message
    assert "data root was resolved to" in message
    assert config.DATA_ROOT_ENV in message


def test_the_console_entry_point_is_declared():
    """An installed wheel should be runnable as a command, not only as `python -m`."""
    text = (config.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project.scripts]" in text
    assert "propx-roofs = \"propx_roofs.cli:main\"" in text


def test_the_package_data_declaration_is_still_in_pyproject():
    """The install above proves the effect; this names the cause, so a deletion is obvious."""
    text = (config.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.setuptools.package-data]" in text
    assert 'propx_roofs = ["schema/*.json", "configs/*.yaml"]' in text


def test_the_dev_extra_supplies_the_local_build_tooling():
    """``--no-build-isolation`` moves the build requirements from pip to the environment.

    Without build isolation pip will not fetch ``setuptools``/``wheel`` itself, so dropping
    either from the ``dev`` extra would not fail here loudly — an unlocked `make install` env
    would just start skipping the build fixtures, and the packaging guarantee would quietly
    stop being checked there. This asserts the dependencies that keep the preflight green in
    both supported setups, so the link is visible in one place.

    The list checked here is the whole toolchain ``[build-system].requires`` names, not just the
    part that happened to be missing once.
    """
    text = (config.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dev_line = next(
        (line for line in text.splitlines() if line.strip().startswith("dev = [")), None
    )
    assert dev_line is not None, "the dev optional-dependency group is gone"
    for requirement in ("setuptools", "wheel"):
        assert requirement in dev_line, (
            "tests/test_packaging.py installs with --no-build-isolation, so the local build "
            f"toolchain must be in the dev extra; `{requirement}` is missing from: "
            f"{dev_line.strip()}"
        )


def test_the_lock_pins_the_exact_build_toolchain():
    """requirements.lock claims to capture everything needed to build AND run. `pip freeze`
    omits pip/setuptools/wheel, so their pins are maintained by hand (see the lock's header);
    this test is what makes forgetting them loud. It also keeps `_environment_matches_lock`
    meaningful — that helper defines 'locked' as matching exactly these pins."""
    pins = _lock_toolchain_pins()
    assert sorted(pins) == sorted(TOOLCHAIN), (
        f"requirements.lock must pin the full build toolchain {sorted(TOOLCHAIN)} with exact "
        f"`==` pins; found only {sorted(pins) or 'none of them'}. Regenerate with the two "
        f"commands in the lock's header (pip freeze omits these three)."
    )


def test_the_build_system_floor_matches_the_preflights_floor():
    """The preflight enforces SETUPTOOLS_FLOOR; pyproject declares the floor for isolated
    builds. If one moves without the other, the preflight stops proving what [build-system]
    demands — so pin them together here."""
    text = (config.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    floor = ".".join(str(part) for part in SETUPTOOLS_FLOOR)
    assert f'requires = ["setuptools>={floor}"]' in text


# --- packaged default configs (RTI-002) -------------------------------------------------------


@pytest.mark.parametrize("name", ["study_area.yaml", "pipeline.yaml"])
def test_the_packaged_default_configs_are_byte_identical_to_the_canonical_ones(name):
    """configs/ at the repo root is canonical; the copies packaged as package data are a
    last-resort fallback for an installed wheel. Byte-identity is asserted so they cannot
    drift: if this fails, re-sync with `cp configs/*.yaml src/propx_roofs/configs/`."""
    canonical = (config.REPO_ROOT / "configs" / name).read_bytes()
    packaged = (config.REPO_ROOT / "src" / PACKAGE / "configs" / name).read_bytes()
    assert packaged == canonical, (
        f"src/{PACKAGE}/configs/{name} differs from the canonical configs/{name}; "
        f"re-sync with `cp configs/{name} src/{PACKAGE}/configs/{name}`"
    )


def test_the_package_version_matches_pyproject():
    """run.pipeline_version reports __version__; a drifted pyproject would ship a wheel that
    disagrees with its own provenance stamp."""
    import propx_roofs

    text = (config.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version_line = next(
        line for line in text.splitlines() if line.strip().startswith("version = ")
    )
    assert version_line.split("=", 1)[1].strip().strip('"') == propx_roofs.__version__


def test_an_installed_package_with_no_checkout_falls_back_to_the_packaged_configs(
    installed_prefix, tmp_path
):
    """RTI-002: from a bare directory, with no env var and no checkout, config.load() must
    still produce the default configuration - from the copies inside the wheel."""
    result = _in_installed_package(
        installed_prefix,
        """
        from propx_roofs import config
        cfg = config.load()
        print(cfg.study_area.name)
        print(cfg.study_area_path)
        """,
        tmp_path,
    )
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "sonnwendviertel"
    # The config actually read must be the packaged copy, not anything under the checkout.
    assert str(installed_prefix) in lines[1]


def test_a_missing_cache_from_an_installed_package_names_the_escape_hatches(
    installed_prefix, tmp_path
):
    """The cache cannot be packaged, so the failure must say how to point at one."""
    result = _in_installed_package(
        installed_prefix,
        """
        from propx_roofs import config, pipeline
        cfg = config.load()
        try:
            pipeline.verify_cache(cfg)
        except pipeline.PipelineError as error:
            message = str(error)
            assert "--cache-root" in message, message
            assert config.DATA_ROOT_ENV in message, message
            assert "cache-build" in message, message
            print("REFUSED-WITH-GUIDANCE")
        else:
            raise SystemExit("verify_cache found a cache where there is none")
        """,
        tmp_path,
    )
    assert "REFUSED-WITH-GUIDANCE" in result.stdout


# --- wheel smoke test (RTI-002 / RTI-022) -----------------------------------------------------


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory) -> Path:
    """Build a real wheel from a copy of the sources — offline, with the preflighted local
    backend (--no-build-isolation --no-index). With the preflight green, a build error is a
    FAILURE; the only permitted skip is the preflight's own (missing backend, unlocked env)."""
    _require_local_build_backend()
    source = tmp_path_factory.mktemp("wheel-source")
    wheel_dir = tmp_path_factory.mktemp("wheel-dist")
    _copy_sources(source)
    argv = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-build-isolation",
        "--no-index",
        "--no-deps",
        "--disable-pip-version-check",
        "-w",
        str(wheel_dir),
        ".",
    ]
    result = _run(argv, cwd=source)
    if result.returncode != 0:
        pytest.fail(
            "building the wheel is a purely local, offline operation (--no-build-isolation "
            "--no-index --no-deps) with a preflighted backend, so this failure is real.\n"
            f"stdout tail:\n{result.stdout[-1500:]}\nstderr tail:\n{result.stderr[-1500:]}"
        )
    wheels = sorted(wheel_dir.glob("*.whl"))
    assert wheels, f"pip wheel exited 0 but produced no wheel in {wheel_dir}"
    assert len(wheels) == 1, f"expected exactly one wheel (--no-deps), got {wheels}"
    return wheels[0]


def _declared_name_and_version() -> tuple[str, str]:
    """The [project] name and version from pyproject.toml, wheel-normalised (PEP 427: runs of
    ``-``/``_``/``.`` in the distribution name become a single ``_``)."""
    text = (config.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    name = re.search(r'^name = "([^"]+)"$', text, flags=re.M).group(1)
    version = re.search(r'^version = "([^"]+)"$', text, flags=re.M).group(1)
    return re.sub(r"[-_.]+", "_", name), version


def test_the_built_wheel_is_not_the_empty_unknown_wheel(built_wheel):
    """Regression for the UNKNOWN-0.0.0 incident, asserted on the artefact itself.

    A pre-PEP-621 setuptools (59.6.0 when it happened here) cannot read [project] from
    pyproject.toml, so it builds a wheel named ``UNKNOWN-0.0.0`` containing only dist-info and
    no package — with exit code 0 and pip printing "Successfully installed". The preflight now
    prevents that build from running at all, but this test is the backstop that inspects what
    was actually built: dist-info name/version must be the declared ones
    (``propx_rooftop_intelligence-0.2.0`` today) and a nonempty ``propx_roofs/`` — including
    the schema — must be inside the wheel.
    """
    name, version = _declared_name_and_version()
    assert name == "propx_rooftop_intelligence" and version != "0.0.0"

    expected_dist_info = f"{name}-{version}.dist-info"
    assert built_wheel.name.startswith(f"{name}-{version}-"), (
        f"wheel filename {built_wheel.name!r} does not carry the declared name/version "
        f"{name}-{version}; an UNKNOWN-0.0.0 name means the backend never read [project]"
    )
    with zipfile.ZipFile(built_wheel) as archive:
        names = archive.namelist()
        dist_infos = {entry.split("/", 1)[0] for entry in names if ".dist-info/" in entry}
        assert dist_infos == {expected_dist_info}, (
            f"expected exactly {expected_dist_info!r} inside the wheel, found {dist_infos}"
        )
        metadata_text = archive.read(f"{expected_dist_info}/METADATA").decode("utf-8")
        assert "Name: propx-rooftop-intelligence" in metadata_text
        assert f"Version: {version}" in metadata_text
        package_entries = [entry for entry in names if entry.startswith(f"{PACKAGE}/")]
        assert package_entries, (
            f"the wheel contains no {PACKAGE}/ package — an empty wheel built with exit 0, "
            f"the exact UNKNOWN-0.0.0 failure mode; contents: {sorted(names)[:20]}"
        )
        assert f"{PACKAGE}/schema/{SCHEMA_RESOURCE}" in names


def test_the_wheel_installs_and_the_cli_runs_from_outside_the_checkout(
    built_wheel, tmp_path_factory
):
    """The full RTI-002 claim: non-editable install, executed from a foreign cwd, finding the
    data only through PROPX_ROOFS_DATA_ROOT (env var) or --cache-root (flag).

    Offline by construction: the wheel is supplied via --no-index --find-links on its own
    directory, --no-deps keeps pip from resolving anything else, and the runtime dependencies
    (numpy, shapely, OpenCV, ...) come from this interpreter's own site-packages at run time —
    zero network requests anywhere.
    """
    prefix = tmp_path_factory.mktemp("wheel-prefix")
    outside = tmp_path_factory.mktemp("outside-the-checkout")

    install = _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(built_wheel.parent),
            "--no-deps",
            "--disable-pip-version-check",
            "--target",
            str(prefix),
            str(built_wheel),
        ],
        cwd=outside,
    )
    assert install.returncode == 0, (
        f"installing the built wheel is a purely local operation and failed:\n"
        f"{install.stdout}\n{install.stderr}"
    )
    # Belt and braces on top of --no-index: pip must not have fetched anything.
    assert "Downloading" not in install.stdout, (
        f"the offline wheel install downloaded something:\n{install.stdout}"
    )
    script = prefix / "bin" / "propx-roofs"
    assert script.is_file(), (
        f"the console entry point was not installed; bin/ holds: "
        f"{sorted(p.name for p in (prefix / 'bin').iterdir()) if (prefix / 'bin').is_dir() else []}"
    )

    base_env = {"PYTHONPATH": str(prefix), "PATH": "/usr/bin:/bin"}

    # The installed CLI, from a foreign cwd, with the data root supplied by the env var.
    result = _run(
        [sys.executable, str(script), "cache-verify"],
        cwd=outside,
        env={**base_env, config.DATA_ROOT_ENV: str(config.REPO_ROOT)},
    )
    assert result.returncode == 0, (
        f"installed `propx-roofs cache-verify` failed outside the checkout:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "cache OK" in result.stdout

    # Same, but with NO env var at all: configs come from the packaged copies and the cache
    # location is given explicitly - the installed-wheel escape hatch.
    result = _run(
        [
            sys.executable,
            str(script),
            "--cache-root",
            str(config.REPO_ROOT / "data" / "cache"),
            "cache-verify",
        ],
        cwd=outside,
        env=base_env,
    )
    assert result.returncode == 0, (
        f"installed `propx-roofs --cache-root ... cache-verify` failed with no env var:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "cache OK" in result.stdout
