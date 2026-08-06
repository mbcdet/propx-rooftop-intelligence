"""Run the solar detector over the evaluation rows. Read-only with respect to the pipeline.

**Nothing in this file edits the detector.** Parameters are varied through the ``image.tuning``
block that ``attributes.param()`` already prefers over ``ATTRIBUTE_PARAMS``, so a search can run
without a single byte of ``src/`` changing and ``algorithm_parameters_hash`` cannot move by
accident. The published pipeline is not invoked and ``outputs/`` is never written.

**Held-out protection.** ``--split held_out`` refuses to run without
``--score-the-held-out-split-once``. The pre-registration allows those rows to be scored exactly
once; an accidental peek during tuning would void the evaluation, so the guard is a flag rather
than a comment.

Two row sources:

* the 109 evaluation rows, from ``labels.csv`` + ``data/eval_cache/<zone>/``;
* the 10 pinned assessment buildings, from ``configs/study_area.yaml`` + ``data/cache/<area>/``,
  used for the out-of-sample positive check on ``vie-swv-001`` / ``vie-swv-003``.
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parent
REPO = EVAL.parents[1]
sys.path.insert(0, str(REPO / "src"))

from propx_roofs import config as config_mod  # noqa: E402
from propx_roofs.attributes.solar import observe_solar_panels  # noqa: E402
from propx_roofs.imaging import build_crop  # noqa: E402

EVAL_CACHE = REPO / "data" / "eval_cache"
ZONE_DIR = {
    "Spengergasse, 1050": "spengergasse",
    "TU Wien / Karlsplatz, 1040": "tu_karlsplatz",
    "WU Wien campus, Welthandelsplatz 1, 1020": "wu_wien",
}

# The six parameters Phase 4 is allowed to search, and nothing else.
SEARCHABLE = (
    "solar_rectangularity_min",
    "solar_periodicity_min",
    "solar_internal_texture_min",
    "solar_orientation_tolerance_deg",
    "solar_open_m",
    "solar_close_m",
)


def load_rows() -> list[dict]:
    lines = (EVAL / "labels.csv").read_text().splitlines(True)
    text = "".join(line for line in lines if not line.startswith("#"))
    return list(csv.DictReader(io.StringIO(text)))


def load_splits() -> dict:
    return json.loads((EVAL / "splits.json").read_text())


def _geometries(cache_dir: Path) -> dict[int, dict]:
    """Roof geometries by OBJECTID. The eval zones and the committed study-area cache name the
    same ``ANLAGENLEISTUNG2025OGD`` layer differently, so both filenames are accepted."""
    for name in ("roof_records_in_bbox.geojson", "roof_records_2025.geojson"):
        path = cache_dir / name
        if path.exists():
            features = json.loads(path.read_text(encoding="utf-8"))["features"]
            return {int(f["properties"]["OBJECTID"]): f["geometry"] for f in features}
    raise SystemExit(f"no cached roof records under {cache_dir}")


class Detector:
    """The solar detector with an ``image.tuning`` override, over a set of pre-built crops.

    Crops are built once and reused across every configuration in a search: assembling them is
    the expensive part and the imagery does not depend on the parameters.
    """

    def __init__(self) -> None:
        self.cfg = config_mod.load()
        self.base = self.cfg.thresholds
        self._crops: dict[tuple[str, int], object] = {}
        self._geoms: dict[str, dict[int, dict]] = {}

    def crop(self, cache_dir: Path, objectid: int):
        key = (str(cache_dir), objectid)
        if key not in self._crops:
            if str(cache_dir) not in self._geoms:
                self._geoms[str(cache_dir)] = _geometries(cache_dir)
            geom = self._geoms[str(cache_dir)][objectid]
            self._crops[key] = build_crop(geom, cache_dir, self.base)
        return self._crops[key]

    def thresholds(self, overrides: dict[str, float] | None):
        if not overrides:
            return self.base
        unknown = set(overrides) - set(SEARCHABLE)
        if unknown:
            raise SystemExit(f"refusing to vary parameters outside the Phase 4 space: {unknown}")
        merged = copy.deepcopy(self.base)
        merged.setdefault("image", {})["tuning"] = dict(overrides)
        return merged

    def run(self, cache_dir: Path, objectid: int, overrides=None) -> dict:
        obs = observe_solar_panels(self.crop(cache_dir, objectid), self.thresholds(overrides))
        return {"objectid": objectid, "value": obs.value, "quality": obs.quality,
                "rationale": obs.rationale}


def eval_targets(split: str) -> list[tuple[Path, int, str, dict]]:
    """``(cache_dir, objectid, reference_label, row)`` for one split of the evaluation rows."""
    rows = {int(r["objectid"]): r for r in load_rows()}
    splits = load_splits()
    if split == "tune":
        ids = splits["tune_objectids"]
    elif split == "held_out":
        ids = splits["held_out_objectids"]
    elif split == "scoreable":
        ids = splits["tune_objectids"] + splits["held_out_objectids"]
    elif split == "dropped":
        ids = [int(o) for group in splits["dropped"].values() for o in group]
    else:
        raise SystemExit(f"unknown split {split!r}")
    out = []
    for oid in ids:
        row = rows[oid]
        out.append((EVAL_CACHE / ZONE_DIR[row["zone"]], oid, row["reference_label"], row))
    return out


def pinned_targets(cfg) -> list[tuple[Path, int, str]]:
    """``(cache_dir, objectid, building_id)`` for the ten pinned assessment buildings."""
    area = cfg.study_area
    return [(Path(area.cache_dir), b.objectid, b.building_id) for b in area.buildings]


def confusion(results: list[tuple[str, bool | None]]) -> dict:
    """``detected`` is ``value is True``; ``None`` (abstained) is not a detection."""
    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0, "abstain_pos": 0, "abstain_neg": 0}
    for reference, value in results:
        fired = value is True
        if reference == "true":
            counts["tp" if fired else "fn"] += 1
            if value is None:
                counts["abstain_pos"] += 1
        else:
            counts["fp" if fired else "tn"] += 1
            if value is None:
                counts["abstain_neg"] += 1
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="tune",
                    choices=["tune", "held_out", "scoreable", "dropped", "pinned"])
    ap.add_argument("--params", default=None, help="JSON dict of image.tuning overrides")
    ap.add_argument("--score-the-held-out-split-once", action="store_true")
    ap.add_argument("--json", default=None, help="write full per-row results here")
    args = ap.parse_args()

    if args.split == "held_out" and not args.score_the_held_out_split_once:
        raise SystemExit(
            "the held-out split is scored ONCE, after tuning is frozen (preregistration §1).\n"
            "Pass --score-the-held-out-split-once if that is what this is."
        )

    overrides = json.loads(args.params) if args.params else None
    det = Detector()
    records = []
    if args.split == "pinned":
        for cache_dir, oid, building_id in pinned_targets(det.cfg):
            r = det.run(cache_dir, oid, overrides)
            r["building_id"] = building_id
            records.append(r)
    else:
        for cache_dir, oid, reference, row in eval_targets(args.split):
            r = det.run(cache_dir, oid, overrides)
            r.update(reference_label=reference, adresse=row["adresse"],
                     drop_reason=row["drop_reason"])
            records.append(r)

    fired = [r for r in records if r["value"] is True]
    print(f"split={args.split}  n={len(records)}  fired={len(fired)}")
    if args.split != "pinned":
        print(json.dumps(confusion([(r["reference_label"], r["value"]) for r in records])))
    for r in records:
        if r["value"] is True or args.split in ("pinned", "dropped"):
            name = r.get("building_id") or r["objectid"]
            print(f"  {name}  value={r['value']}  {r.get('adresse', '')}")
    if args.json:
        Path(args.json).write_text(json.dumps(records, indent=1))


if __name__ == "__main__":
    main()
