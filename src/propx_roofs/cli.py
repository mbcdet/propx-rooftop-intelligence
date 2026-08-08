"""Command line entry point. Offline by construction, with one explicit exception.

    propx-roofs cache-verify     # is the committed cache fit to run?   (offline)
    propx-roofs run              # the full pipeline, no network        (offline)
    propx-roofs release-check    # artifacts vs manifest/schema/cache + audit binding (offline)
    propx-roofs review-checklist # write the human-review checklist     (offline)
    propx-roofs record-review    # a HUMAN records a completed review   (offline, deliberate)
    propx-roofs cache-build      # rebuild the study-area cache — THE ONE NETWORKED COMMAND

``cache-verify`` and ``run`` cannot reach the network. ``cache-build`` is the single fetching
entry point in the package: it rebuilds ``data/cache/<area>/`` from Vienna's live WFS/WMTS
services via :mod:`propx_roofs.cache_build` and then verifies every byte it wrote.
``pipeline.verify_cache`` calls that module's verifier and never its fetcher. (``tools/`` keeps
``recon.py`` for optional multi-area reconnaissance and a ``build_cache.py`` shim for the
historical invocation.)

Exit codes are the contract with ``make`` and with CI: **0 only when a validated document was
written.** A schema violation, a missing pinned record or a failed cache check all exit
non-zero and print no success line, because a green run that produced an invalid file is worse
than a red one.

Every run stamps two hashes into ``run`` — ``run.config_hash`` and
``run.algorithm_parameters_hash``. They answer different questions and are deliberately not
combined:

``config_hash``
    sha256 of ``configs/study_area.yaml`` + ``configs/pipeline.yaml``. Answers *were the same
    inputs and the same tunable thresholds used?* It moves when a building is pinned or
    unpinned, or when a YAML threshold is edited.

``algorithm_parameters_hash``
    Fingerprint of the tracked values in ``SEGMENT_PARAMS`` and ``ATTRIBUTE_PARAMS``. Answers
    *were the same tracked in-code algorithm parameter values used?* It moves when one of those
    constants is edited in Python — which ``config_hash`` cannot see, because no YAML changed.

    It is **not** a fingerprint of the source code, the dependencies or the runtime. Editing
    algorithm *logic*, upgrading OpenCV or NumPy, or changing Python version all leave it
    untouched.

Matching both hashes is **necessary but not sufficient** for bitwise reproduction: two runs can
agree on both and still differ, because the code, the installed libraries or the interpreter
differ. Neither is a version number; they detect parameter drift, they do not order it and they
do not certify a reproduction. What the hashes cannot see, the run records beside them:
``run.git`` (commit + dirty flag, or an honest null), ``run.runtime`` and ``run.dependencies``
— so a reader can judge whether a byte-identical reproduction is expected (only under
``requirements.lock`` on the pinned platform) or a semantic one
(``python3 -m propx_roofs.semantic_compare``, any supported environment).
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
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=None,
        help=(
            "directory holding the study-area cache (the parent of <area>/manifest.json); "
            f"defaults to <data root>/data/cache, where the data root is the checkout or "
            f"${config.DATA_ROOT_ENV}. The cache is not packaged with the wheel."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("cache-verify", help="verify the committed study-area cache (offline)")

    sub.add_parser(
        "cache-hash",
        help=(
            "maintenance/migration (offline): write sha256 content hashes for the EXISTING "
            "cache files into the manifest, then verify. The manifest records that the hashes "
            "were computed offline on the given date from the already-fetched cache - they pin "
            "the content forward, they do NOT claim fetch-time verification. A fresh "
            "cache-build writes fetch-time hashes and does not need this."
        ),
    )

    sub.add_parser(
        "cache-build",
        help=(
            "rebuild the study-area cache from Vienna's live WFS/WMTS services, then verify "
            "it. THE ONE NETWORKED COMMAND in this package; cache-verify and run are offline."
        ),
    )

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
        help=(
            "fix run.generated_at for reproduction checks; code, dependencies and runtime may "
            "still cause differences"
        ),
    )
    run_parser.add_argument(
        "--audit-annotations",
        type=Path,
        default=None,
        help=(
            "machine-readable visual-audit transcription to attach as manual_review report "
            "blocks and review flags (default: the checkout's "
            "validation/audit_annotations.json when it exists). Report only: annotations "
            "cannot change any value, geometry or confidence."
        ),
    )
    run_parser.add_argument(
        "--no-audit-annotations",
        action="store_true",
        help="attach no audit annotations even if the default file exists",
    )

    release_parser = sub.add_parser(
        "release-check",
        help=(
            "verify an output directory against its artifact manifest, the packaged schema "
            "and the cache on disk, and report the visual-audit binding honestly (offline). "
            "Exit 0 = artifacts consistent (the audit binding may still be baseline-only, "
            "reported as an explicit warning); 1 = an artifact, manifest, schema or cache "
            "inconsistency; 3 (only with --require-current-audit) = consistent but awaiting "
            "a recorded human review of the current artifacts."
        ),
    )
    release_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"directory holding roof_attributes.json (default {DEFAULT_OUTPUT_DIR})",
    )
    release_parser.add_argument(
        "--audit-annotations",
        type=Path,
        default=None,
        help=(
            "annotations file recording what the visual audit examined (default: the "
            "checkout's validation/audit_annotations.json when it exists)"
        ),
    )
    release_parser.add_argument(
        "--require-current-audit",
        action="store_true",
        help=(
            "strict final-release mode: fail with exit code 3 unless audit_basis records a "
            "completed human review of the CURRENT artifacts (artifact sha256, every "
            "overlay's sha256, reviewer, date - written by record-review). Exit 3 means "
            "'artifacts consistent but awaiting current-artifact review', distinct from "
            "1 = inconsistency."
        ),
    )

    record_parser = sub.add_parser(
        "record-review",
        help=(
            "record a completed HUMAN review of the current artifacts into the annotations "
            "file's audit_basis (offline). Running this ASSERTS that the named reviewer "
            "actually examined the artifacts; it computes and stores the artifact and "
            "overlay sha256s, it cannot check that anyone looked. Run it only after working "
            "through validation/final_review_checklist.md."
        ),
    )
    record_parser.add_argument(
        "--reviewer", required=True, help="name of the human who performed the review"
    )
    record_parser.add_argument(
        "--date",
        default=None,
        help="review date (ISO); defaults to today",
    )
    record_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"directory holding the reviewed artifacts (default {DEFAULT_OUTPUT_DIR})",
    )
    record_parser.add_argument(
        "--annotations",
        type=Path,
        default=None,
        help=(
            "annotations file to write the review record into (default: the checkout's "
            "validation/audit_annotations.json)"
        ),
    )

    checklist_parser = sub.add_parser(
        "review-checklist",
        help=(
            "write the compact human-review checklist for the current artifacts to "
            "validation/final_review_checklist.md (offline, deterministic: regenerating "
            "against unchanged artifacts is byte-identical). No checkbox is ever pre-filled."
        ),
    )
    checklist_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"directory holding roof_attributes.json (default {DEFAULT_OUTPUT_DIR})",
    )
    checklist_parser.add_argument(
        "--out",
        type=Path,
        default=config.REPO_ROOT / "validation" / "final_review_checklist.md",
        help="where to write the checklist (default validation/final_review_checklist.md)",
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
        cfg = pipeline.load_config(args.study_area, args.pipeline_config, args.cache_root)
    except (OSError, ValueError, KeyError) as error:
        logger.error("configuration is unusable: %s", error)
        return 2

    if args.command == "cache-build":
        # The single networked command. Deliberately loud about it, so an offline environment
        # failing here is understood as expected rather than as a pipeline defect.
        from . import cache_build

        print("cache-build fetches from Vienna's live services - this is the ONE network step")
        try:
            manifest = cache_build.fetch(cfg)
        except (cache_build.CacheError, OSError) as error:
            logger.error("cache build failed: %s", error)
            return 1
        print(f"fetched {manifest['counts']['tiles']} tiles; verifying the cache it wrote")
        if cache_build.verify(cfg):
            logger.error("the freshly built cache did NOT verify; do not run against it")
            return 1
        print(f"cache OK: {cfg.study_area.cache_dir}")
        return 0

    if args.command == "cache-hash":
        # Offline maintenance: pins the already-fetched cache content with sha256 hashes and
        # records honestly that they were computed after the fact, not at fetch time.
        from . import cache_build

        try:
            integrity = cache_build.cache_hash(cfg)
        except (cache_build.CacheError, OSError) as error:
            logger.error("cache-hash failed: %s", error)
            return 1
        print(
            f"hashed {len(integrity['vectors'])} vector file(s) and "
            f"{len(integrity['tiles'])} tile(s) into "
            f"{cfg.study_area.cache_dir / 'manifest.json'}"
        )
        print(f"basis: {integrity['basis']}")
        if integrity.get("note"):
            print(f"note : {integrity['note']}")
        if cache_build.verify(cfg):
            logger.error("cache did NOT verify against the freshly written hashes")
            return 1
        print(f"cache OK: {cfg.study_area.cache_dir}")
        return 0

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

    if args.command == "release-check":
        # Offline verification that the published artifacts are exactly the ones the run
        # fingerprinted, and an honest report of what the visual audit did and did not see.
        from . import audit

        report = audit.release_check(
            cfg,
            args.output_dir,
            args.audit_annotations,
            require_current_audit=args.require_current_audit,
        )
        for line in report.info:
            print(f"ok      : {line}")
        for line in report.warnings:
            print(f"WARNING : {line}")
        for line in report.awaiting_review:
            print(f"AWAITING: {line}", file=sys.stderr)
        for line in report.problems:
            print(f"PROBLEM : {line}", file=sys.stderr)
        if not report.ok:
            logger.error(
                "release-check FAILED: %d problem(s) in %s", len(report.problems),
                args.output_dir,
            )
        elif report.awaiting_review:
            logger.error(
                "release-check STRICT: artifacts in %s are internally consistent but "
                "AWAITING a completed human review of the current artifacts (exit 3)",
                args.output_dir,
            )
        else:
            print(
                f"release-check PASS: artifacts in {args.output_dir} are internally "
                f"consistent (audit_binding: {report.audit_binding})"
            )
        return report.exit_code

    if args.command == "record-review":
        from . import audit

        annotations_path = (
            args.annotations
            if args.annotations is not None
            else audit.DEFAULT_ANNOTATIONS_PATH
        )
        print(
            "record-review: running this command ASSERTS that the named human reviewer "
            "actually examined the current artifacts (document + every overlay, per "
            "validation/final_review_checklist.md). It records that assertion with the "
            "artifact hashes; it cannot verify that anyone looked. If the review did not "
            "happen, abort now - a fabricated review record is worse than none."
        )
        try:
            recorded = audit.record_review(
                args.output_dir, annotations_path, args.reviewer, args.date
            )
        except audit.AuditError as error:
            logger.error("record-review refused: %s", error)
            return 1
        print(f"recorded into {annotations_path}:")
        print(f"  reaudit_reviewer            : {recorded['reaudit_reviewer']}")
        print(f"  reaudit_date                : {recorded['reaudit_date']}")
        print(f"  reaudit_of_artifact_sha256  : {recorded['reaudit_of_artifact_sha256']}")
        for name, digest in recorded["reaudit_overlays"].items():
            print(f"  reaudit_overlays[{name}] : {digest}")
        print(
            "verify with: propx-roofs release-check --require-current-audit "
            f"--output-dir {args.output_dir}"
        )
        return 0

    if args.command == "review-checklist":
        from . import audit

        try:
            text = audit.review_checklist(args.output_dir)
        except audit.AuditError as error:
            logger.error("review-checklist refused: %s", error)
            return 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(
            f"wrote {args.out} for the artifacts in {args.output_dir}; work through it, "
            f"then run: propx-roofs record-review --reviewer \"Name\""
        )
        return 0

    if args.no_audit_annotations:
        annotations: object = None
    elif args.audit_annotations is not None:
        annotations = args.audit_annotations
    else:
        annotations = pipeline.AUDIT_ANNOTATIONS_DEFAULT

    try:
        document = pipeline.run(
            cfg,
            generated_at=args.generated_at or datetime.now().astimezone(),
            output_dir=args.output_dir,
            write_overlays=not args.no_overlays,
            audit_annotations=annotations,
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
    # Printed rather than buried in the JSON: these are what a reviewer compares between two
    # runs, and the module docstring explains why one hash is not enough.
    repro = document["run"]
    print(
        f"config_hash {repro['config_hash']} "
        f"(inputs + YAML thresholds)  "
        f"algorithm_parameters_hash {repro['algorithm_parameters_hash']} "
        f"(tracked in-code algorithm parameter values); matching both is necessary but not "
        f"sufficient for bitwise reproduction - code, dependencies and runtime are not hashed, "
        f"but are recorded in run.git / run.runtime / run.dependencies"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
