.PHONY: install install-locked recon cache-build cache-verify run verify-repro verify-repro-semantic test lint clean

PY ?= python3
OUTPUT_DIR ?= outputs

# src/ on the path so a fresh checkout runs without `make install` first. Harmless when the
# package is installed editable, which resolves to the same files.
PKG_PY = PYTHONPATH=src:$$PYTHONPATH $(PY)

install:
	$(PY) -m pip install -e '.[dev]'

# The pinned environment (requirements.lock, resolved on Python 3.11.3 / macOS arm64) that the
# byte-reproducibility claim is made for. `install` above is the UNLOCKED path: it resolves
# fresh lower-bounded deps (with normal build isolation) and is expected to reproduce the
# outputs semantically, not byte-for-byte.
#
# The lock also pins the build toolchain (pip/setuptools/wheel), so installing it first is the
# whole toolchain setup — no unbounded `pip install --upgrade pip`. The editable install then
# builds with --no-build-isolation: against exactly the setuptools the lock just pinned, never
# an isolated environment resolving unpinned build deps.
install-locked:
	$(PY) -m pip install -r requirements.lock
	$(PY) -m pip install --no-build-isolation -e .

# Phase 2 reconnaissance. Needs network access to mapsneu.wien.gv.at and data.wien.gv.at.
recon:
	$(PY) tools/recon.py

# Rebuild the study-area cache from the live Vienna services. THE ONE NETWORKED COMMAND the
# package ships; everything below it is offline.
cache-build:
	$(PKG_PY) -m propx_roofs.cli cache-build

# Is the committed cache fit to run? Offline. Non-zero exit if not.
cache-verify:
	$(PKG_PY) -m propx_roofs.cli cache-verify

# The offline, cache-backed pipeline. OFFLINE: it reads only data/cache/ and refuses to start if that
# cache does not verify. Writes outputs/roof_attributes.json and outputs/overlays/*.png, and
# exits non-zero without a success line if the output fails schema validation.
run: cache-verify
	$(PKG_PY) -m propx_roofs.cli run --output-dir $(OUTPUT_DIR)

# BYTE-level reproduction check. This is only expected to hold in the LOCKED environment
# (requirements.lock: CPython 3.11 on macOS arm64) that produced the committed artefacts; in any
# other environment use verify-repro-semantic below instead. Offline. Runs into a temporary
# directory and never touches outputs/, reuses the committed run's generated_at so the timestamp
# alone cannot cause a difference, and compares the JSON byte-for-byte apart from run.git (which
# necessarily records the checkout state at generation time). Overlays are compared byte-for-byte
# with no exclusions.
#
# A failure means the bytes differ here and now; the cause has to be investigated and may be any
# of: stale committed artefacts, code or config drift since they were written, a different
# dependency or interpreter version, or genuine nondeterminism in the pipeline. Exits non-zero so
# the difference cannot be missed.
verify-repro: cache-verify
	@dir=$$(mktemp -d) && trap 'rm -rf "$$dir"' EXIT && \
	gen=$$($(PY) -c "import json;print(json.load(open('outputs/roof_attributes.json'))['run']['generated_at'])") && \
	$(PKG_PY) -m propx_roofs.cli run --output-dir "$$dir" --generated-at "$$gen" >/dev/null && \
	$(PKG_PY) -m propx_roofs.semantic_compare --bytes outputs/roof_attributes.json "$$dir/roof_attributes.json" && \
	diff -r -q outputs/overlays "$$dir/overlays" && \
	echo "byte-level reproduction OK in this environment (expected to hold only under requirements.lock)"

# SEMANTIC reproduction check: a fresh run must agree with the committed outputs on every
# published attribute value, availability, review flag and geometry coordinate (tolerance 1e-9).
# run.generated_at/git/runtime/dependencies are excluded - they describe the act of running.
# This is the reproducibility level claimed for any supported environment.
verify-repro-semantic: cache-verify
	@dir=$$(mktemp -d) && trap 'rm -rf "$$dir"' EXIT && \
	$(PKG_PY) -m propx_roofs.cli run --output-dir "$$dir" >/dev/null && \
	$(PKG_PY) -m propx_roofs.semantic_compare outputs/roof_attributes.json "$$dir/roof_attributes.json"

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tools tests

clean:
	rm -rf outputs/recon .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
