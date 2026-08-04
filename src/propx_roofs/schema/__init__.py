"""The output contract, and the one function that enforces it.

``outputs/roof_attributes.json`` is validated against the committed schema and a failure fails
the run (design section 8). The schema is stricter than a serialiser needs it to be, on purpose:
``additionalProperties: false`` almost everywhere means a renamed or misspelled field is a hard
error rather than a silently dropped value, and the ``const`` entries make several design rules
unrepresentable in a valid document — the primary polygon cannot be the CV candidate, a
confidence cannot lose its "not a calibrated probability" note, and a source cannot lose its
licence or attribution.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).with_name("roof_attributes.schema.json")

MAX_REPORTED_ERRORS = 8


class SchemaValidationError(ValueError):
    """Raised with every failing path listed, so one run surfaces more than one mistake."""


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = load_schema()
    Draft202012Validator.check_schema(schema)  # a broken schema must not read as a valid document
    return Draft202012Validator(schema)


def _path(parts: Any) -> str:
    out = "$"
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def validate(document: Any) -> None:
    """Validate a whole output document. Returns ``None``; raises on the first failing run.

    Errors are sorted by location and reported together with their JSON path, because a
    schema failure is meant to be fixable from the message alone.
    """
    errors = sorted(_validator().iter_errors(document), key=lambda e: list(e.absolute_path))
    if not errors:
        return
    lines = [f"{_path(e.absolute_path)}: {e.message}" for e in errors[:MAX_REPORTED_ERRORS]]
    if len(errors) > MAX_REPORTED_ERRORS:
        lines.append(f"... and {len(errors) - MAX_REPORTED_ERRORS} more")
    raise SchemaValidationError(
        f"{len(errors)} schema violation(s) against {SCHEMA_PATH.name}:\n  " + "\n  ".join(lines)
    )
