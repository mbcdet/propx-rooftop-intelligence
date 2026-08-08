"""Semantic comparison of two ``roof_attributes.json`` documents.

Reproducibility here has **two distinct levels**, and this module is the tool for the first:

* **Semantic reproducibility** — the scientific content agrees: published attribute values,
  availability, review flags, geometry (within a tiny coordinate tolerance), hashes and the
  schema version. This is what any supported environment must reproduce.
* **Byte reproducibility** — the serialized artefacts are identical. This is claimed **only**
  for the pinned environment in ``requirements.lock`` that produced the committed outputs
  (``make verify-repro``); other dependency versions may legitimately change low-order digits
  of image diagnostics without changing any published value.

What is compared, and how:

* ``run.generated_at``, ``run.git``, ``run.runtime`` and ``run.dependencies`` are **excluded**:
  they record *when/what produced* the document, not what it says, and two honest reproductions
  differ there by construction.
* Human ``reaudit_*`` bindings inside ``manual_review.audit_basis`` are also excluded. They are
  written after an artifact is produced and bind a reviewer to those bytes; including them in a
  fresh run would create an unavoidable self-reference. Baseline-audit identity and all review
  findings remain part of the semantic comparison.
* Published attribute ``value`` fields, availability, flags and strings must match exactly;
  numbers anywhere are compared with ``--tolerance`` (default ``1e-9``, i.e. semantic equality
  including geometry coordinates).
* Diagnostics under ``image_evidence.quality`` and the ``delineation`` agreement metrics are
  compared with ``--diagnostics-tolerance`` (defaults to ``--tolerance``). Loosen only that
  knob when comparing across dependency versions whose CV numerics drift: it widens the
  allowance **only** for diagnostics, never for published values.

Exit status: 0 when semantically equivalent, 1 with a readable difference list otherwise, 2 on
unusable input. ``--bytes`` performs the byte-level check instead (JSON identical apart from
``run.git``, which necessarily records the checkout state at generation time), and
``--self-check FILE`` re-validates a document against the packaged schema.

    python3 -m propx_roofs.semantic_compare outputs/roof_attributes.json fresh/roof_attributes.json
    python3 -m propx_roofs.semantic_compare --bytes outputs/... fresh/...
    python3 -m propx_roofs.semantic_compare --self-check outputs/roof_attributes.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

#: run-level fields that describe the act of running, not the result. Never compared.
EXCLUDED_RUN_FIELDS = ("generated_at", "git", "runtime", "dependencies")

#: Post-publication review metadata. These fields bind a human review to already-written bytes,
#: so they cannot be reproduced by the run that creates those bytes. Only these fields, and only
#: below a manual-review audit_basis, are excluded; the baseline audit trail remains compared.
EXCLUDED_REAUDIT_FIELDS = (
    "reaudit_of_artifact_sha256",
    "reaudit_overlays",
    "reaudit_reviewer",
    "reaudit_date",
)

#: The exact serialization pipeline.write_outputs uses. Byte mode re-serializes with this to
#: prove the comparison it makes is about the same bytes the pipeline writes.
def _serialize(document: Any) -> str:
    return json.dumps(document, ensure_ascii=False, indent=1) + "\n"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_diagnostic_path(path: tuple[Any, ...]) -> bool:
    """True for the subtrees where cross-version CV drift is tolerated (with its own knob).

    * anything under an ``image_evidence.quality`` block (shadow fractions, approximate pixel
      areas, texture scores — measured on the pixel grid, sensitive to OpenCV/NumPy version);
    * the ``delineation`` agreement metrics (IoU, Hausdorff, gradient ratios) for the same
      reason. Published attribute ``value`` fields never live under either subtree.
    """
    for first, second in zip(path, path[1:], strict=False):
        if first == "image_evidence" and second == "quality":
            return True
    return "delineation" in path


def _fmt_path(path: tuple[Any, ...]) -> str:
    out = "$"
    for part in path:
        # Brackets for list indices and for keys that are not plain identifiers (building ids
        # like "vie-swv-001"), so paths read the same way here as in the schema validator.
        if isinstance(part, int) or not str(part).isidentifier():
            out += f"[{part}]"
        else:
            out += f".{part}"
    return out


def _walk(
    a: Any,
    b: Any,
    path: tuple[Any, ...],
    diffs: list[str],
    tolerance: float,
    diagnostics_tolerance: float,
) -> None:
    where = _fmt_path(path)

    if _is_number(a) and _is_number(b):
        allowed = diagnostics_tolerance if _is_diagnostic_path(path) else tolerance
        both_finite = math.isfinite(float(a)) and math.isfinite(float(b))
        if (not both_finite and str(a) != str(b)) or (
            both_finite and abs(float(a) - float(b)) > allowed
        ):
            kind = "diagnostic" if _is_diagnostic_path(path) else "value"
            diffs.append(
                f"{where}: {kind} differs: {a!r} != {b!r} "
                f"(|delta| {abs(float(a) - float(b)):.3g} > tolerance {allowed:g})"
            )
        return

    if type(a) is not type(b) and not (_is_number(a) and _is_number(b)):
        diffs.append(f"{where}: type differs: {type(a).__name__} != {type(b).__name__}")
        return

    if isinstance(a, dict):
        keys = set(a) | set(b)
        if path and path[-1] == "audit_basis" and "manual_review" in path:
            keys -= set(EXCLUDED_REAUDIT_FIELDS)
        for key in sorted(keys, key=str):
            if key not in a:
                diffs.append(f"{_fmt_path((*path, key))}: only in second document")
            elif key not in b:
                diffs.append(f"{_fmt_path((*path, key))}: only in first document")
            else:
                _walk(a[key], b[key], (*path, key), diffs, tolerance, diagnostics_tolerance)
        return

    if isinstance(a, list):
        if len(a) != len(b):
            diffs.append(f"{where}: list length differs: {len(a)} != {len(b)}")
        # strict=False: a length mismatch is already reported above; compare the overlap.
        for index, (item_a, item_b) in enumerate(zip(a, b, strict=False)):
            _walk(item_a, item_b, (*path, index), diffs, tolerance, diagnostics_tolerance)
        return

    if a != b:
        diffs.append(f"{where}: {a!r} != {b!r}")


def compare(
    first: dict[str, Any],
    second: dict[str, Any],
    *,
    tolerance: float = 1e-9,
    diagnostics_tolerance: float | None = None,
) -> list[str]:
    """All semantic differences between two documents, as readable path-prefixed lines.

    Empty list means semantically equivalent. ``run.generated_at``/``git``/``runtime``/
    ``dependencies`` are excluded (see the module docstring); everything else is compared.
    """
    if diagnostics_tolerance is None:
        diagnostics_tolerance = tolerance

    diffs: list[str] = []

    run_a = {k: v for k, v in dict(first.get("run") or {}).items() if k not in EXCLUDED_RUN_FIELDS}
    run_b = {
        k: v for k, v in dict(second.get("run") or {}).items() if k not in EXCLUDED_RUN_FIELDS
    }
    _walk(run_a, run_b, ("run",), diffs, tolerance, diagnostics_tolerance)

    buildings_a = {b.get("building_id"): b for b in first.get("buildings", [])}
    buildings_b = {b.get("building_id"): b for b in second.get("buildings", [])}
    for missing in sorted(set(buildings_a) - set(buildings_b), key=str):
        diffs.append(f"$.buildings[{missing}]: only in first document")
    for missing in sorted(set(buildings_b) - set(buildings_a), key=str):
        diffs.append(f"$.buildings[{missing}]: only in second document")
    for building_id in sorted(set(buildings_a) & set(buildings_b), key=str):
        _walk(
            buildings_a[building_id],
            buildings_b[building_id],
            ("buildings", str(building_id)),
            diffs,
            tolerance,
            diagnostics_tolerance,
        )
    return diffs


def byte_compare(first_path: Path, second_path: Path) -> list[str]:
    """The byte-level check: identical bytes, apart from ``run.git``.

    ``run.git`` necessarily records the checkout state (commit, dirty flag) at generation
    time, so a byte comparison against committed artefacts masks exactly that one object and
    nothing else. To keep "masked equality" meaningful as a statement about bytes, each file
    must first equal its **own** canonical re-serialization (the pipeline's exact serializer);
    then equality of the masked canonical forms is equality of the original bytes everywhere
    outside ``run.git``.
    """
    problems: list[str] = []
    documents = []
    for path in (first_path, second_path):
        raw = path.read_text(encoding="utf-8")
        document = json.loads(raw)
        if _serialize(document) != raw:
            problems.append(
                f"{path}: not in the pipeline's canonical serialization; a byte-level "
                f"verdict about it would be meaningless"
            )
        documents.append(document)
    if problems:
        return problems

    for document in documents:
        if isinstance(document.get("run"), dict):
            document["run"]["git"] = None
    if _serialize(documents[0]) != _serialize(documents[1]):
        problems.append(
            "documents differ beyond run.git; semantic difference list follows:"
        )
        problems.extend(compare(documents[0], documents[1], tolerance=0.0) or ["(none found)"])
    return problems


def self_check(path: Path) -> list[str]:
    """Re-validate one document against the packaged schema. Empty list means valid."""
    from .schema import SchemaValidationError, validate

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"{path}: unreadable: {error}"]
    try:
        validate(document)
    except SchemaValidationError as error:
        return [str(error)]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="propx_roofs.semantic_compare", description=__doc__)
    parser.add_argument("first", nargs="?", type=Path, help="reference document")
    parser.add_argument("second", nargs="?", type=Path, help="document to compare against it")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1e-9,
        help="absolute tolerance for every number, including geometry coordinates and "
        "published values (default 1e-9: semantic equality)",
    )
    parser.add_argument(
        "--diagnostics-tolerance",
        type=float,
        default=None,
        help="looser absolute tolerance applied ONLY to image_evidence.quality diagnostics "
        "and delineation metrics, for comparisons across dependency versions whose CV "
        "numerics drift (default: same as --tolerance)",
    )
    parser.add_argument(
        "--bytes",
        action="store_true",
        help="byte-level check instead: identical bytes apart from run.git (expected to hold "
        "only in the pinned requirements.lock environment)",
    )
    parser.add_argument(
        "--self-check",
        type=Path,
        default=None,
        metavar="FILE",
        help="validate one document against the packaged schema and exit",
    )
    args = parser.parse_args(argv)

    if args.self_check is not None:
        problems = self_check(args.self_check)
        if problems:
            print(f"SCHEMA-INVALID: {args.self_check}", file=sys.stderr)
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1
        print(f"schema-valid: {args.self_check}")
        return 0

    if args.first is None or args.second is None:
        parser.error("two documents are required unless --self-check is used")

    if args.bytes:
        problems = byte_compare(args.first, args.second)
        if problems:
            print(f"NOT byte-identical: {args.first} vs {args.second}", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
        print(
            f"byte-identical apart from run.git (which records checkout state): "
            f"{args.first} vs {args.second}"
        )
        return 0

    try:
        first = json.loads(args.first.read_text(encoding="utf-8"))
        second = json.loads(args.second.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"cannot read input: {error}", file=sys.stderr)
        return 2

    diffs = compare(
        first,
        second,
        tolerance=args.tolerance,
        diagnostics_tolerance=args.diagnostics_tolerance,
    )
    if diffs:
        print(
            f"SEMANTIC DIFFERENCE: {len(diffs)} difference(s) between "
            f"{args.first} and {args.second}",
            file=sys.stderr,
        )
        for diff in diffs:
            print(f"  {diff}", file=sys.stderr)
        return 1
    print(
        f"semantically equivalent: {args.first} vs {args.second} "
        f"(tolerance {args.tolerance:g}; run.generated_at/git/runtime/dependencies excluded)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
