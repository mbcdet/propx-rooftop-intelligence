.PHONY: install recon test lint clean

PY ?= python3

install:
	$(PY) -m pip install -e '.[dev]'

# Phase 2 reconnaissance. Needs network access to mapsneu.wien.gv.at and data.wien.gv.at.
recon:
	$(PY) tools/recon.py

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tools tests

clean:
	rm -rf outputs/recon .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
