.PHONY: install recon cache-verify run verify-repro test lint clean

PY ?= python3
OUTPUT_DIR ?= outputs

# src/ on the path so a fresh checkout runs without `make install` first. Harmless when the
# package is installed editable, which resolves to the same files.
PKG_PY = PYTHONPATH=src:$$PYTHONPATH $(PY)

install:
	$(PY) -m pip install -e '.[dev]'

# Phase 2 reconnaissance. Needs network access to mapsneu.wien.gv.at and data.wien.gv.at.
recon:
	$(PY) tools/recon.py

# Is the committed cache fit to run? Offline. Non-zero exit if not.
cache-verify:
	$(PKG_PY) -m propx_roofs.cli cache-verify

# The offline, cache-backed pipeline. OFFLINE: it reads only data/cache/ and refuses to start if that
# cache does not verify. Writes outputs/roof_attributes.json and outputs/overlays/*.png, and
# exits non-zero without a success line if the output fails schema validation.
run: cache-verify
	$(PKG_PY) -m propx_roofs.cli run --output-dir $(OUTPUT_DIR)

# Check byte-for-byte reproduction of the committed outputs IN THE CURRENT INSTALLED ENVIRONMENT.
# Offline. Runs into a temporary directory and never touches outputs/, and reuses the committed
# run's generated_at so that timestamp alone cannot cause a difference.
#
# This does NOT prove reproducibility in general. A failure means the bytes differ here and now;
# the cause has to be investigated and may be any of: stale committed artefacts, code or config
# drift since they were written, a different dependency or interpreter version, or genuine
# nondeterminism in the pipeline. Exits non-zero so the difference cannot be missed.
verify-repro: cache-verify
	@dir=$$(mktemp -d) && trap 'rm -rf "$$dir"' EXIT && \
	gen=$$($(PY) -c "import json;print(json.load(open('outputs/roof_attributes.json'))['run']['generated_at'])") && \
	$(PKG_PY) -m propx_roofs.cli run --output-dir "$$dir" --generated-at "$$gen" >/dev/null && \
	diff -q outputs/roof_attributes.json "$$dir/roof_attributes.json" && \
	diff -r -q outputs/overlays "$$dir/overlays" && \
	echo "byte-identical to the committed outputs in this environment (roof_attributes.json + 10 overlays)"

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tools tests

clean:
	rm -rf outputs/recon .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
