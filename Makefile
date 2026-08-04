.PHONY: install recon cache-verify run test lint clean

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

# The deterministic pipeline. OFFLINE: it reads only data/cache/ and refuses to start if that
# cache does not verify. Writes outputs/roof_attributes.json and outputs/overlays/*.png, and
# exits non-zero without a success line if the output fails schema validation.
run: cache-verify
	$(PKG_PY) -m propx_roofs.cli run --output-dir $(OUTPUT_DIR)

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tools tests

clean:
	rm -rf outputs/recon .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
