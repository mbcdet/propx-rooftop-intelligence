"""Command line entry point. Offline by construction.

    python3 -m propx_roofs.cli cache-verify   # is the committed cache fit to run?
    python3 -m propx_roofs.cli run            # the full pipeline, no network

Neither subcommand can reach the network: the only fetch path in the repository is
``tools/build_cache.py``, and nothing here imports it except through
``pipeline.verify_cache``, which calls the verifier and never the fetcher.

Exit codes are the contract with ``make`` and with CI: **0 only when a validated document was
written.** A schema violation, a missing pinned record or a failed cache check all exit
non-zero and print no success line, because a green run that produced an invalid file is worse
than a red one.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Imported as a module, not by name: every call below goes through ``pipeline.<fn>``, so a test
# (or a caller) that substitutes a stage sees the substitution take effect here too. Binding the
# functions directly would silently pin this module to the originals.
from . import config, pipeline
from .schema import SchemaValidationError

DEFAULT_OUTPUT_DIR = config.REPO_ROOT / "outputs"

logger = logging.getLogger("propx_roofs")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="propx_roofs.cli", description=__doc__)
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log every stage at DEBUG level"
    )
    parser.add_argument(
        "--study-area", type=Path, default=None, help="override configs/study_area.yaml"
    )
    parser.add_argument(
        "--pipeline-config", type=Path, default=None, help="override configs/pipeline.yaml"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("cache-verify", help="verify the committed study-area cache (offline)")

    run_parser = sub.add_parser("run", help="run the offline pipeline and write outputs/")
    run_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"where to write roof_attributes.json and overlays/ (default {DEFAULT_OUTPUT_DIR})",
    )
    run_parser.add_argument(
        "--no-overlays", action="store_true", help="skip the per-building overlay PNGs"
    )
    run_parser.add_argument(
        "--generated-at",
        default=None,
        help="fix run.generated_at; two runs with the same value are byte-identical",
    )
    return parser


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _configure_logging(args.verbose)

    try:
        cfg = pipeline.load_config(args.study_area, args.pipeline_config)
    except (OSError, ValueError, KeyError) as error:
        logger.error("configuration is unusable: %s", error)
        return 2

    if args.command == "cache-verify":
        try:
            problems = pipeline.verify_cache(cfg)
        except pipeline.PipelineError as error:
            logger.error("%s", error)
            return 2
        if problems:
            logger.error("cache is NOT fit to run: %d problem(s)", len(problems))
            return 1
        print(f"cache OK: {cfg.study_area.cache_dir}")
        return 0

    try:
        document = pipeline.run(
            cfg,
            generated_at=args.generated_at or datetime.now().astimezone(),
            output_dir=args.output_dir,
            write_overlays=not args.no_overlays,
        )
    except pipeline.PipelineError as error:
        logger.error("%s", error)
        return 1
    except SchemaValidationError as error:
        # Explicit: an invalid document is a failed run. Nothing was reported as success and
        # the caller sees a non-zero exit (design section 8).
        logger.error("output failed schema validation, so the run failed:\n%s", error)
        return 1

    output_dir = Path(args.output_dir)
    print(
        f"wrote {output_dir / 'roof_attributes.json'} "
        f"({len(document['buildings'])} buildings, schema-validated)"
    )
    if not args.no_overlays:
        print(f"wrote {len(document['buildings'])} overlay(s) to {output_dir / 'overlays'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
