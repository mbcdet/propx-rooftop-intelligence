"""Footprint-seeded segmentation and its agreement with the authoritative outline.

**Cadastre-seeded refinement and QC, not independent detection.** The segmenter is initialised
from the authoritative outline (see :mod:`propx_roofs.segment.grabcut`), so the candidate
polygon is a constrained refinement of that seed — it is free to move only within the trimap
band around the outline and inherits everything else from it. Consequently every agreement
metric in :mod:`propx_roofs.segment.agreement` compares two statistically **dependent**
estimates: high IoU means the imagery did not contradict the cadastre near its boundary, and
must never be read as independent confirmation of the cadastre or as segmentation accuracy.
The published records say this in their ``note`` field; this docstring is the code-side
statement of the same design decision.

Design section 4 is unambiguous about the division of labour: **authoritative geodata provides
delineation; CV provides consistency evidence and an alignment diagnostic.** So this package can
produce a candidate polygon and a set of agreement numbers, and it has no code path that returns
the candidate as a primary outline.

``SEGMENT_PARAMS`` holds the algorithm-shape parameters — kernel radii, an iteration count, a
simplification tolerance, the sanity band on area. They are stated in **ground metres** or as
ratios so they mean the same thing at any zoom, they are named and commented in this one place
rather than inlined, and :func:`param` reads a ``segment:`` block from ``configs/pipeline.yaml``
in preference to them if one is ever added. They are not the published decision thresholds:
those live in the config file and are read from it directly.
"""

from __future__ import annotations

from typing import Any

from ..imaging import threshold

SEGMENT_PARAMS: dict[str, float] = {
    # Trimap. The band has to be wider than the plausible disagreement between the roof record
    # and the imagery: reconnaissance measured a ~2% mutual re-cut between the 2025 outline and
    # the FMZK dissolve (study_area_selection section 3), and eaves, parapets and ortho residual
    # add to that. 1.0 m in, 1.0 m out is ~10 px each way at z20 — enough for GrabCut to have a
    # decision to make, narrow enough that the seed still dominates.
    "trimap_erode_m": 1.0,
    "trimap_dilate_m": 1.0,
    # GrabCut. Fixed count, never "iterate until stable": a data-dependent stopping rule makes
    # the output depend on content in a way that is hard to reproduce.
    "grabcut_iterations": 5.0,
    "grabcut_rng_seed": 20240320.0,  # the first imagery flight date, so the number is traceable
    # Geometric refinement. Close bridges panel gaps and skylight strips inside one roof;
    # 0.6 m is roughly a module width, so it does not swallow a real courtyard.
    "close_m": 0.6,
    # Contour simplification tolerance. At 0.15 m native GSD, 0.45 m is three native pixels:
    # below the resolution of a real building corner, so it removes staircase artefacts of
    # rasterisation without cutting a genuine step in the outline.
    "simplify_native_gsd_multiple": 3.0,
    # Interior rings are recovered, not dropped: two of the ten selected buildings have courtyard
    # voids, and a candidate that fills them would report a positive area bias against an
    # authoritative outline that correctly excludes them. 4 m2 is about a light well; below that a
    # hole is a segmentation speck rather than a void.
    "min_hole_area_m2": 4.0,
    # A crop assembled over a cache gap is uniform black there, so a boundary drawn across it
    # would be fabricated and the agreement numbers would describe our cache. Below this share of
    # roof pixels carrying real imagery the candidate is refused outright.
    "min_roof_data_fraction": 0.75,
    # Degenerate-result band on cv_area / authoritative_area. Wide on purpose: this rejects
    # collapse and runaway, and is not a quality judgement. Anything inside the band is
    # reported with its agreement numbers and left to the reader.
    "area_ratio_min": 0.40,
    "area_ratio_max": 2.50,
    # boundary_alignment_warning trigger (design section 4.2). Cautious: it fires only on
    # material divergence, and it says nothing about the authoritative data being wrong.
    "warn_iou_below": 0.70,
    "warn_hausdorff_above_m": 5.0,
    # Hausdorff densification: shapely's discrete Hausdorff only visits vertices, so a long
    # simplified edge would be under-measured against a dense one.
    "hausdorff_densify": 0.05,
    # Boundary gradient sampling width, in ground metres. One native pixel on each side of the
    # line, so the sample sits on the edge rather than averaging over a band.
    "boundary_sample_width_m": 0.3,
}

SEGMENTER_NAME = "grabcut+geometric_refinement"


def param(cfg: Any, name: str) -> float:
    """Algorithm parameter for the segmenter: ``segment.<name>`` from config, else the default."""
    if name not in SEGMENT_PARAMS:
        raise KeyError(f"unknown segment parameter {name!r}")
    return float(threshold(cfg, "segment", name, default=SEGMENT_PARAMS[name]))
