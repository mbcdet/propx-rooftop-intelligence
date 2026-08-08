#!/usr/bin/env bash
# The one deterministic finalization command for the pinned Mac host (macOS arm64, Python 3.11).
#
# Runs every check the submission claims, in order, fail-fast, and regenerates outputs/ for
# real. It never commits and never pushes - the human does that after reading the summary.
#
# Reproducibility levels exercised here:
#   step 7: SEMANTIC  - a second run must agree on every published value (any supported env)
#   step 8: BYTE      - a third run must be byte-identical apart from run.git (claimed ONLY in
#                       this pinned requirements.lock environment, which this script builds)
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"
PYTHON="${PYTHON:-python3.11}"
VENV="$REPO/.venv"

step() { printf '\n==> %s\n' "$*"; }

step "[1/10] venv at .venv from requirements.lock (the pinned byte-repro environment)"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
fi
# No unbounded `pip install --upgrade pip`: requirements.lock pins the build toolchain
# (pip/setuptools/wheel) alongside the runtime dependencies, so installing the lock IS the
# toolchain install — a fresh venv's bundled pip upgrades itself to exactly the pinned version.
"$VENV/bin/python" -m pip install --quiet --require-virtualenv --disable-pip-version-check \
  -r requirements.lock
# Preflight: the three toolchain versions must match the lock exactly. Everything after this
# point builds with --no-build-isolation, i.e. against THIS toolchain, so a mismatch here would
# silently void the pinned-environment claim — fail loudly instead.
"$VENV/bin/python" - <<'EOF'
import re
import sys
from importlib import metadata

assert sys.version_info[:2] == (3, 11), f"finalization requires Python 3.11, got {sys.version}"
pins = dict(
    re.match(r"(pip|setuptools|wheel)==(\S+)", line).groups()
    for line in open("requirements.lock", encoding="utf-8")
    if re.match(r"(pip|setuptools|wheel)==", line)
)
assert sorted(pins) == ["pip", "setuptools", "wheel"], (
    f"requirements.lock no longer pins the full build toolchain: {pins}"
)
mismatches = {
    name: (pinned, metadata.version(name))
    for name, pinned in pins.items()
    if metadata.version(name) != pinned
}
assert not mismatches, (
    "build toolchain differs from requirements.lock (installed vs pinned): "
    + ", ".join(f"{n}: {got} != {want}" for n, (want, got) in mismatches.items())
)
print(f"python {sys.version.split()[0]} at {sys.executable}")
print("toolchain matches lock: " + ", ".join(f"{n}=={v}" for n, v in sorted(pins.items())))
EOF
# The preflight above proved the local backend satisfies [build-system] (setuptools pinned at
# its exact locked version, well above the >=68 floor), so the editable build may — and must —
# run without isolation: no unpinned build dependencies are ever resolved.
"$VENV/bin/python" -m pip install --quiet --no-build-isolation -e '.[dev]'

step "[2/10] ruff"
"$VENV/bin/python" -m ruff check src tools tests

step "[3/10] full test suite"
"$VENV/bin/python" -m pytest -q

step "[4/10] cache-verify (offline)"
"$VENV/bin/propx-roofs" cache-verify

step "[5/10] regenerate outputs/ (real run, overlays on)"
"$VENV/bin/propx-roofs" run --output-dir outputs

step "[6/10] re-validate outputs/roof_attributes.json against the packaged schema"
"$VENV/bin/python" -m propx_roofs.semantic_compare --self-check outputs/roof_attributes.json

step "[7/10] SEMANTIC repro: second run into a temp dir, compared semantically"
SEMDIR="$(mktemp -d)"
trap 'rm -rf "$SEMDIR" "${BYTEDIR:-}" "${WHEELDIR:-}" "${WHEELVENV:-}" "${OUTSIDE:-}"' EXIT
"$VENV/bin/propx-roofs" run --output-dir "$SEMDIR" >/dev/null
"$VENV/bin/python" -m propx_roofs.semantic_compare outputs/roof_attributes.json "$SEMDIR/roof_attributes.json"

step "[8/10] BYTE repro: third run, byte-compared (expected to pass in this pinned env)"
BYTEDIR="$(mktemp -d)"
GEN="$("$VENV/bin/python" -c "import json;print(json.load(open('outputs/roof_attributes.json'))['run']['generated_at'])")"
"$VENV/bin/propx-roofs" run --output-dir "$BYTEDIR" --generated-at "$GEN" >/dev/null
"$VENV/bin/python" -m propx_roofs.semantic_compare --bytes outputs/roof_attributes.json "$BYTEDIR/roof_attributes.json"
diff -r -q outputs/overlays "$BYTEDIR/overlays"
echo "overlays byte-identical"

step "[9/10] wheel: build, install non-editable into a temp venv, run from outside the checkout"
WHEELDIR="$(mktemp -d)"
# --no-build-isolation: the step-1 preflight proved the local toolchain matches the lock and
# satisfies [build-system], so the wheel is built by the pinned local backend rather than by an
# isolated environment that would resolve unpinned build dependencies (and need network).
"$VENV/bin/python" -m pip wheel --quiet --no-deps --no-build-isolation -w "$WHEELDIR" .
WHEEL="$(ls "$WHEELDIR"/*.whl)"
WHEELVENV="$(mktemp -d)/venv"
"$PYTHON" -m venv "$WHEELVENV"
"$WHEELVENV/bin/python" -m pip install --quiet --disable-pip-version-check "$WHEEL" \
  -r "$REPO/requirements.lock"
OUTSIDE="$(mktemp -d)"
(cd "$OUTSIDE" && PROPX_ROOFS_DATA_ROOT="$REPO" "$WHEELVENV/bin/propx-roofs" cache-verify)

step "[10/10] release gate: manifest consistency, then the STRICT current-audit gate"
# Non-strict must pass: artifacts byte-match their manifest, schema holds, cache hash matches.
"$VENV/bin/python" -m propx_roofs.cli release-check
# The checklist is regenerated against exactly these artifacts, so the reviewer signs what
# was actually produced.
"$VENV/bin/python" -m propx_roofs.cli review-checklist
# Strict mode is the SUBMISSION gate. Exit 3 means the artifacts are consistent but no human
# has recorded a review of these exact bytes yet - expected until Mohammad reviews
# validation/final_review_checklist.md and runs `propx-roofs record-review --reviewer ...`.
# Exit 1 means an inconsistency and fails this script. Exit 0 means the gate is fully passed.
set +e
"$VENV/bin/python" -m propx_roofs.cli release-check --require-current-audit
STRICT=$?
set -e
case "$STRICT" in
  0) RELEASE_LINE='release     : STRICT gate PASSED - a human review of these exact artifacts is recorded' ;;
  3) RELEASE_LINE='release     : AWAITING FINAL HUMAN REVIEW - work through validation/final_review_checklist.md, then run: propx-roofs record-review --reviewer "<name>"' ;;
  *) echo "release-check --require-current-audit failed with an inconsistency (exit $STRICT)"; exit "$STRICT" ;;
esac

printf '\n================= finalize: ALL CHECKS PASSED =================\n'
printf 'environment : %s (requirements.lock)\n' "$("$VENV/bin/python" -c 'import platform;print(platform.python_version(), platform.platform())')"
printf 'outputs     : regenerated in outputs/ (JSON schema-valid, %s overlays)\n' "$(ls outputs/overlays/*.png | wc -l | tr -d ' ')"
printf 'semantic    : second run agrees on every published value\n'
printf 'byte        : third run byte-identical apart from run.git; overlays identical\n'
printf 'wheel       : %s installs and runs outside the checkout\n' "$(basename "$WHEEL")"
printf '%s\n' "$RELEASE_LINE"
printf 'NOT done    : no git commit, no push - review the diff and commit deliberately\n'
