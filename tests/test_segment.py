"""GrabCut baseline and the agreement block.

The helpers come from ``test_imaging`` rather than a shared fixture module so that the synthetic
crop is defined exactly once; pytest puts the tests directory on ``sys.path``.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Polygon
from test_imaging import (
    CENTRE_LAT,
    CENTRE_LON,
    CFG,
    override,
    square_wgs84,
    synthetic_crop,
    usable_tile_cache,
)

from propx_roofs.segment import agreement, grabcut

# The two published thresholds, written out rather than read from the config: a test that takes
# its expectation from the same file as the code cannot catch the file being misread.
WARN_IOU_BELOW = 0.70
WARN_HAUSDORFF_ABOVE_M = 5.0

ROOF_ATTRIBUTES = Path(__file__).resolve().parents[1] / "outputs" / "roof_attributes.json"


def bright_roof(size: int = 300, margin: int = 60) -> tuple[np.ndarray, np.ndarray]:
    """A bright textured rectangle on a dark background, with the outline that seeds it."""
    rng = np.random.default_rng(7)
    rgb = np.full((size, size, 3), 40, np.uint8)
    rgb[margin:-margin, margin:-margin] = 200
    rgb = np.clip(rgb.astype(np.int16) + rng.integers(-6, 7, rgb.shape), 0, 255).astype(np.uint8)
    mask = np.zeros((size, size), bool)
    mask[margin:-margin, margin:-margin] = True
    return rgb, mask


def test_segmentation_is_byte_identical_across_runs() -> None:
    """GrabCut seeds its GMMs with k-means++ from the global OpenCV RNG; the seed is fixed."""
    rgb, mask = bright_roof()
    first, meta_first = grabcut.segment(synthetic_crop(rgb, mask), CFG)
    second, meta_second = grabcut.segment(synthetic_crop(rgb, mask), CFG)
    assert first is not None and second is not None
    assert hashlib.sha256(first.tobytes()).hexdigest() == (
        hashlib.sha256(second.tobytes()).hexdigest()
    )
    assert meta_first["polygon"] == meta_second["polygon"]
    assert meta_first["grabcut_rng_seed"] == meta_second["grabcut_rng_seed"]


def test_segmentation_recovers_a_bright_rectangle() -> None:
    rgb, mask = bright_roof()
    crop = synthetic_crop(rgb, mask)
    result, meta = grabcut.segment(crop, CFG)
    assert meta["failure_reason"] is None
    assert result is not None
    overlap = (result & mask).sum() / (result | mask).sum()
    assert overlap > 0.9, f"seeded segmentation should track its seed closely, got {overlap:.3f}"
    assert 3 <= meta["n_vertices"] <= 20, "simplification should leave a compact polygon"


def two_part_roof(size: int = 400) -> tuple[np.ndarray, np.ndarray]:
    """Two bright textured rectangles on a dark background: a MultiPolygon building."""
    rng = np.random.default_rng(7)
    rgb = np.full((size, size, 3), 40, np.uint8)
    mask = np.zeros((size, size), bool)
    for x0, x1 in ((60, 170), (230, 340)):
        rgb[100:300, x0:x1] = 200
        mask[100:300, x0:x1] = True
    rgb = np.clip(rgb.astype(np.int16) + rng.integers(-6, 7, rgb.shape), 0, 255).astype(np.uint8)
    return rgb, mask


def test_a_two_component_roof_keeps_both_components() -> None:
    """RTI-010: 'largest component only' silently discarded every other part of a MultiPolygon
    roof while the whole-roof area gate could still pass. Both parts must survive, the polygon
    must be a MultiPolygon, and the per-part recall must be published."""
    rgb, mask = two_part_roof()
    crop = synthetic_crop(rgb, mask)
    result, meta = grabcut.segment(crop, CFG)
    assert meta["failure_reason"] is None
    assert result is not None
    assert meta["n_input_components"] == 2
    assert meta["n_output_components"] == 2
    assert meta["multipolygon_input"] is True
    assert meta["polygon"]["type"] == "MultiPolygon"
    assert len(meta["polygon"]["coordinates"]) == 2
    assert len(meta["input_component_recall"]) == 2
    for recall in meta["input_component_recall"]:
        assert recall > 0.8, f"a component was substantially lost: {meta['input_component_recall']}"
    assert "component_count_warning" not in meta
    # Both parts really are in the pixel mask, not only in the metadata.
    left = result[:, :200].sum()
    right = result[:, 200:].sum()
    assert left > 0 and right > 0

    # And the agreement stage accepts the MultiPolygon candidate without special-casing.
    comparison = agreement.compare(meta["polygon"], _two_part_polygon(crop), crop, CFG)
    assert comparison.failure_reason is None
    assert comparison.iou > 0.8


def _two_part_polygon(crop) -> dict:
    """GeoJSON MultiPolygon matching ``two_part_roof``'s outline."""
    from propx_roofs.imaging import rings_to_polygon_wgs84

    parts = []
    for x0, x1 in ((60, 170), (230, 340)):
        ring = np.array([[x0, 100], [x1, 100], [x1, 300], [x0, 300]], dtype=np.int32)
        polygon = rings_to_polygon_wgs84(crop, ring)
        assert polygon is not None
        parts.append(polygon["coordinates"])
    return {"type": "MultiPolygon", "coordinates": parts}


def test_components_touching_no_input_part_are_removed_as_noise() -> None:
    """Unit test for the retention rule: a foreground blob far from every input component is
    noise and must be dropped; components near any input part are all kept."""
    foreground = np.zeros((200, 200), np.uint8)
    foreground[20:60, 20:60] = 255  # overlaps the seed
    foreground[100:140, 20:60] = 255  # near (touches the dilated seed)
    foreground[150:190, 150:190] = 255  # far from everything: noise
    near = np.zeros((200, 200), bool)
    near[20:105, 20:60] = True  # dilated authoritative outline
    kept, n_kept, n_dropped = grabcut._components_near_seed(foreground, near)
    assert n_kept == 2
    assert n_dropped == 1
    assert kept is not None
    assert kept[30, 30] and kept[120, 30]
    assert not kept[170, 170], "the far blob must be removed as noise"

    nothing, none_kept, all_dropped = grabcut._components_near_seed(
        foreground, np.zeros((200, 200), bool)
    )
    assert nothing is None and none_kept == 0 and all_dropped == 3


def test_concurrent_segmentation_matches_sequential_results() -> None:
    """RTI-023: cv2.setRNGSeed is process-global, so the seed-then-segment pair runs under a
    module lock. Two threads segmenting different crops concurrently must produce exactly the
    masks the same crops produce sequentially."""
    from concurrent.futures import ThreadPoolExecutor

    rgb_a, mask_a = bright_roof()
    rgb_b, mask_b = two_part_roof()
    crop_a, crop_b = synthetic_crop(rgb_a, mask_a), synthetic_crop(rgb_b, mask_b)

    sequential_a, meta_a = grabcut.segment(crop_a, CFG)
    sequential_b, meta_b = grabcut.segment(crop_b, CFG)
    assert sequential_a is not None and sequential_b is not None

    for _ in range(3):  # a few rounds to give interleaving a chance to happen
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(grabcut.segment, crop_a, CFG)
            future_b = pool.submit(grabcut.segment, crop_b, CFG)
            concurrent_a, concurrent_meta_a = future_a.result()
            concurrent_b, concurrent_meta_b = future_b.result()
        assert hashlib.sha256(concurrent_a.tobytes()).hexdigest() == (
            hashlib.sha256(sequential_a.tobytes()).hexdigest()
        )
        assert hashlib.sha256(concurrent_b.tobytes()).hexdigest() == (
            hashlib.sha256(sequential_b.tobytes()).hexdigest()
        )
        assert concurrent_meta_a["polygon"] == meta_a["polygon"]
        assert concurrent_meta_b["polygon"] == meta_b["polygon"]


def test_degenerate_mask_returns_a_failure_reason_not_an_exception() -> None:
    """A sliver thinner than the erosion band has no sure foreground. That is reportable."""
    rgb = np.full((200, 200, 3), 120, np.uint8)
    sliver = np.zeros((200, 200), bool)
    sliver[100:103, 40:160] = True  # ~0.3 m wide at z20
    result, meta = grabcut.segment(synthetic_crop(rgb, sliver), CFG)
    assert result is None
    assert meta["failure_reason"] == "sure_foreground_empty_after_erosion"

    empty, meta_empty = grabcut.segment(synthetic_crop(rgb, np.zeros((200, 200), bool)), CFG)
    assert empty is None and meta_empty["failure_reason"] == "authoritative_mask_empty"


def test_area_ratio_band_rejects_a_degenerate_result() -> None:
    rgb, mask = bright_roof()
    crop = synthetic_crop(rgb, mask)
    strict = override(override(CFG, ("segment", "area_ratio_min"), 1.5), ("segment",
                                                                         "area_ratio_max"), 2.0)
    result, meta = grabcut.segment(crop, strict)
    assert result is None
    assert meta["failure_reason"] == "area_ratio_outside_sane_band"
    assert meta["area_ratio_vs_authoritative"] is not None


def test_missing_imagery_over_the_roof_is_refused() -> None:
    """GrabCut on a uniform black gap returns its seed, which would look like a good IoU."""
    rgb = np.zeros((300, 300, 3), np.uint8)
    mask = np.zeros((300, 300), bool)
    mask[80:220, 80:220] = True
    layer, zoom = CFG.threshold("imagery", "layer"), CFG.threshold("imagery", "zoom")
    reference = synthetic_crop(rgb, mask)
    from propx_roofs.sources import wmts

    span = reference.pixel_size_m * wmts.TILE_SIZE
    col = int((reference.origin_x + wmts.ORIGIN_SHIFT) // span)
    row = int((wmts.ORIGIN_SHIFT - reference.origin_y) // span)
    tiles = tuple(
        f"{layer}/{zoom}/{row + dr}/{col + dc}" for dr in (0, 1) for dc in (0, 1)
    )
    result, meta = grabcut.segment(synthetic_crop(rgb, mask, missing_tiles=tiles), CFG)
    assert result is None
    assert meta["failure_reason"] == "insufficient_image_data_over_roof"


def test_failed_segmentation_compares_to_a_null_delineation() -> None:
    crop = synthetic_crop(np.full((100, 100, 3), 120, np.uint8))
    result = agreement.compare(None, square_wgs84(10.0), crop, CFG, failure_reason="empty_seg")
    assert result.polygon is None
    assert result.failure_reason == "empty_seg"
    assert result.iou is None and result.hausdorff_m is None
    assert result.as_dict()["note"].startswith("Comparison evidence only")


def test_agreement_metrics_on_a_real_segmentation() -> None:
    rgb, mask = bright_roof()
    crop = synthetic_crop(rgb, mask)
    _, meta = grabcut.segment(crop, CFG)
    authoritative = _mask_polygon(crop, mask)
    result = agreement.compare(meta["polygon"], authoritative, crop, CFG)
    assert result.failure_reason is None
    assert 0.85 <= result.iou <= 1.0
    assert result.symmetric_difference_ratio < 0.3
    assert result.hausdorff_m < 3.0
    assert result.alignment_warning["flag"] is False


def _mask_polygon(crop, mask: np.ndarray) -> dict:
    """GeoJSON outline of a rectangular boolean mask, for use as the authoritative geometry."""
    from propx_roofs.imaging import rings_to_polygon_wgs84

    rows, cols = np.nonzero(mask)
    corners = np.array(
        [
            [cols.min(), rows.min()],
            [cols.max(), rows.min()],
            [cols.max(), rows.max()],
            [cols.min(), rows.max()],
        ],
        dtype=np.int32,
    )
    polygon = rings_to_polygon_wgs84(crop, corners)
    assert polygon is not None
    return polygon


def test_boundary_gradient_ratio_is_higher_on_a_real_edge_than_in_flat_texture() -> None:
    """The one seed-independent signal: it must respond to image structure, not to the seed."""
    size = 400
    rng = np.random.default_rng(3)
    rgb = np.full((size, size, 3), 60, np.int16)
    rgb[:, size // 2 :] = 220  # a single strong vertical edge at x = 200
    # Real orthophoto is never perfectly flat; without a little grain the "flat texture"
    # denominator would be exactly zero and the ratio would be undefined rather than small.
    rgb = np.clip(rgb + rng.integers(-4, 5, rgb.shape), 0, 255).astype(np.uint8)
    crop = synthetic_crop(rgb)

    # Two identically shaped rectangles: one with a long side lying on the step, one entirely
    # inside the bright half. Same perimeter, so only the underlying image differs.
    on_edge = _rect_polygon(crop, 200, 50, 212, 350)
    in_flat = _rect_polygon(crop, 300, 50, 312, 350)
    ratio_edge = agreement.boundary_gradient_ratio(crop, on_edge, in_flat, CFG)
    ratio_flat = agreement.boundary_gradient_ratio(crop, in_flat, on_edge, CFG)

    assert ratio_edge > 2.0, f"a boundary on the edge should score high, got {ratio_edge}"
    assert ratio_flat < 0.5, f"a boundary in flat texture should score low, got {ratio_flat}"
    assert ratio_edge * ratio_flat == pytest.approx(1.0, rel=1e-6)


def test_boundary_gradient_ratio_is_none_on_a_featureless_crop() -> None:
    """A crop with no structure at all — an area filled from missing tiles — has no ratio."""
    crop = synthetic_crop(np.zeros((300, 300, 3), np.uint8))
    a = _rect_polygon(crop, 20, 20, 120, 120)
    b = _rect_polygon(crop, 150, 150, 280, 280)
    assert agreement.boundary_gradient_ratio(crop, a, b, CFG) is None


def _rect_polygon(crop, x0: int, y0: int, x1: int, y1: int) -> dict:
    from propx_roofs.imaging import rings_to_polygon_wgs84

    ring = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.int32)
    polygon = rings_to_polygon_wgs84(crop, ring)
    assert polygon is not None
    return polygon


def test_alignment_warning_is_cautious_and_names_no_fault() -> None:
    warning = agreement.alignment_warning(iou=0.4, hausdorff_m=9.0, sym_ratio=0.8, cfg=CFG)
    assert warning["flag"] is True
    assert warning["status"] == "requires_visual_review"
    assert len(warning["metrics"]["triggers"]) == 2
    # The warning names what it was measured on, so a reader never has to wonder whether a
    # ring-count difference could have contributed to it.
    assert warning["metrics"]["basis"] == "exterior_rings_only"
    assert warning["metrics"]["exterior_iou"] == 0.4
    assert warning["metrics"]["thresholds"] == {
        "iou_below": WARN_IOU_BELOW,
        "hausdorff_above_m": WARN_HAUSDORFF_ABOVE_M,
    }
    note = warning["note"]
    assert "does not indicate that the authoritative data is incorrect or outdated" in note
    quiet = agreement.alignment_warning(iou=0.95, hausdorff_m=0.4, sym_ratio=0.05, cfg=CFG)
    assert quiet == {"flag": False, "metrics": None, "note": None}


# --------------------------------------------------------------------------------------------
# interior rings: edge disagreement measured apart from ring-count disagreement
#
# ``shapely``'s ``.boundary`` of a polygon with holes is the exterior ring *plus* every interior
# ring, so a Hausdorff distance over it conflates "the perimeters sit apart" with "one outline
# records a void the other does not". The candidate segmenter drops holes below
# ``min_hole_area_m2`` by design, so the second condition is guaranteed on any roof with a small
# light shaft and must not be able to fire the alignment warning by itself.


def _flat_crop(size: int = 400) -> object:
    """A featureless crop: these tests are about geometry, so the imagery must contribute nothing.

    ``boundary_gradient_ratio`` is ``None`` on a uniform crop, which is the honest answer and
    keeps the fixtures from asserting anything about pixels.
    """
    return synthetic_crop(np.full((size, size, 3), 120, np.uint8))


def _offset_wgs84(east_m: float, north_m: float) -> tuple[float, float]:
    """A lon/lat offset in metres from the study-area centre, as in ``square_wgs84``."""
    return (
        CENTRE_LON + east_m / (111320.0 * math.cos(math.radians(CENTRE_LAT))),
        CENTRE_LAT + north_m / 110574.0,
    )


def _rect_wgs84(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    """Rectangle given in metres east/north of the study-area centre.

    The degree conversion is the small-angle one, so a length asserted against it is good to a
    fraction of a percent — every assertion below is written with a tolerance to match, because
    the number under test is measured in EPSG:31256 and not in this frame.
    """
    corners = [
        _offset_wgs84(x0, y0),
        _offset_wgs84(x1, y0),
        _offset_wgs84(x1, y1),
        _offset_wgs84(x0, y1),
    ]
    return Polygon(corners)


def test_identical_outlines_with_a_hole_agree_on_the_exterior_and_show_no_mismatch() -> None:
    """The control case: a shared courtyard must not cost anything anywhere."""
    outline = square_wgs84(15.0, hole_m=3.0)  # 30 x 30 m with a 6 x 6 m void
    result = agreement.compare(outline, outline, _flat_crop(), CFG)

    assert result.failure_reason is None
    assert result.exterior_iou == 1.0
    assert result.iou == 1.0
    assert result.hausdorff_m == pytest.approx(0.0, abs=1e-6)
    assert result.hausdorff_full_boundary_m == pytest.approx(0.0, abs=1e-6)

    mismatch = result.topology_mismatch
    assert mismatch["flag"] is False
    assert mismatch["authoritative_interior_rings"] == 1
    assert mismatch["cv_interior_rings"] == 1
    assert (
        mismatch["authoritative_interior_ring_areas_m2"]
        == mismatch["cv_interior_ring_areas_m2"]
    )
    assert mismatch["crs"] == str(CFG.threshold("crs", "metric"))
    assert result.alignment_warning["flag"] is False


def test_a_hole_only_the_authoritative_outline_has_moves_only_the_full_boundary_figure() -> None:
    """The regression this split exists for.

    Same perimeter on both sides, one courtyard on one side. ``hausdorff_m`` must not notice;
    ``hausdorff_full_boundary_m`` must, because the void ring sits 12 m from the perimeter and
    the old single figure reported that 12 m as boundary divergence.
    """
    authoritative = square_wgs84(15.0, hole_m=3.0)
    candidate = square_wgs84(15.0)  # the segmenter did not reproduce the void
    result = agreement.compare(candidate, authoritative, _flat_crop(), CFG)

    assert result.hausdorff_m == pytest.approx(0.0, abs=1e-6), "the exterior rings are identical"
    assert result.hausdorff_full_boundary_m == pytest.approx(12.0, rel=0.02)
    assert result.hausdorff_full_boundary_m > result.hausdorff_m

    # The void is real, so iou still reports it; exterior_iou is what removes it deliberately.
    assert result.exterior_iou == 1.0
    assert result.iou < 1.0

    mismatch = result.topology_mismatch
    assert mismatch["flag"] is True
    assert mismatch["authoritative_interior_rings"] == 1
    assert mismatch["cv_interior_rings"] == 0
    assert mismatch["authoritative_interior_ring_areas_m2"] == [pytest.approx(36.0, rel=0.01)]
    assert mismatch["cv_interior_ring_areas_m2"] == []


def test_diverging_exterior_rings_fire_the_alignment_warning() -> None:
    """A 7 m spur off one wall: high overlap, and a perimeter that genuinely wandered.

    Written so that only the Hausdorff trigger can fire — ``exterior_iou`` stays well above its
    threshold — which is what makes it a test of the exterior-ring measurement rather than of
    the overlap.
    """
    authoritative = square_wgs84(15.0)
    candidate = square_wgs84(15.0).union(_rect_wgs84(15.0, -1.0, 22.0, 1.0))
    result = agreement.compare(candidate, authoritative, _flat_crop(), CFG)

    assert result.hausdorff_m == pytest.approx(7.0, rel=0.02)
    assert result.hausdorff_m > WARN_HAUSDORFF_ABOVE_M
    assert result.exterior_iou > WARN_IOU_BELOW

    warning = result.alignment_warning
    assert warning["flag"] is True
    assert warning["status"] == "requires_visual_review"
    triggers = warning["metrics"]["triggers"]
    assert len(triggers) == 1 and "hausdorff" in triggers[0]
    assert warning["metrics"]["basis"] == "exterior_rings_only"
    assert "does not indicate that the authoritative data is incorrect" in warning["note"]
    assert result.topology_mismatch["flag"] is False


def test_a_topology_mismatch_alone_does_not_fire_the_alignment_warning() -> None:
    """The published defect: a light shaft the segmenter cannot reproduce is not a divergence."""
    authoritative = square_wgs84(15.0, hole_m=1.5)  # a 3 x 3 m shaft, 13.5 m from the perimeter
    candidate = square_wgs84(15.0)
    result = agreement.compare(candidate, authoritative, _flat_crop(), CFG)

    # The all-rings figure crosses the trigger on its own; the warning must not be looking at it.
    assert result.hausdorff_full_boundary_m > WARN_HAUSDORFF_ABOVE_M
    assert result.exterior_iou > WARN_IOU_BELOW
    assert result.alignment_warning == {"flag": False, "metrics": None, "note": None}

    mismatch = result.topology_mismatch
    assert mismatch["flag"] is True
    # A diagnostic, not a route to review: no status of its own, and nothing to route.
    assert "status" not in mismatch
    assert "requires_visual_review" not in json.dumps(mismatch)
    note = mismatch["note"]
    assert "does not indicate that the authoritative data is incorrect or outdated" in note
    assert "does not indicate that the candidate outline is the better one" in note
    assert "expected consequence" in note


def test_a_multipart_outline_is_measured_without_crashing() -> None:
    """Both parts contribute their exterior ring, and a hole in one part is still bookkept."""
    west = _rect_wgs84(-20.0, -8.0, -6.0, 8.0)
    east = _rect_wgs84(6.0, -8.0, 20.0, 8.0)
    west_with_void = Polygon(west.exterior, [_rect_wgs84(-15.0, -3.0, -11.0, 3.0).exterior])
    result = agreement.compare(
        MultiPolygon([west, east]), MultiPolygon([west_with_void, east]), _flat_crop(), CFG
    )

    assert result.failure_reason is None
    assert result.exterior_iou == 1.0
    assert result.hausdorff_m == pytest.approx(0.0, abs=1e-6)
    assert result.hausdorff_full_boundary_m > 3.0
    assert result.topology_mismatch["authoritative_interior_rings"] == 1
    assert result.topology_mismatch["cv_interior_rings"] == 0
    assert result.alignment_warning["flag"] is False


def test_as_dict_publishes_every_value_the_schema_has_to_expect() -> None:
    """The output contract, asserted key by key: the JSON Schema forbids additional properties."""
    result = agreement.compare(
        square_wgs84(15.0), square_wgs84(15.0, hole_m=3.0), _flat_crop(), CFG
    )
    published = result.as_dict()

    assert set(published) == {
        "segmenter",
        "agreement_with_authoritative_geometry",
        "boundary_alignment_warning",
        "topology_mismatch",
        "failure_reason",
        "note",
    }
    assert set(published["agreement_with_authoritative_geometry"]) == {
        "iou",
        "exterior_iou",
        "symmetric_difference_ratio",
        "hausdorff_m",
        "hausdorff_full_boundary_m",
        "boundary_gradient_ratio",
    }
    assert published["topology_mismatch"]["flag"] is True

    # A candidate that never existed publishes the same keys with every metric null, so the
    # shape of the record does not depend on whether segmentation converged.
    failed = agreement.compare(
        None, square_wgs84(15.0), _flat_crop(), CFG, failure_reason="synthetic"
    ).as_dict()
    assert set(failed) == set(published)
    assert all(
        value is None for value in failed["agreement_with_authoritative_geometry"].values()
    )
    assert failed["topology_mismatch"] is None
    assert failed["boundary_alignment_warning"] is None


@pytest.mark.skipif(
    usable_tile_cache() is None or not ROOF_ATTRIBUTES.is_file(),
    reason="needs the committed tile cache and a previous run's outputs/roof_attributes.json",
)
def test_vie_swv_007_agreement_is_not_driven_by_its_two_light_shafts() -> None:
    """The real case that exposed the defect, against the committed run's own polygons.

    vie-swv-007 carries two interior rings of 1.21 and 0.69 m2 — both far below the segmenter's
    4.0 m2 minimum hole area, so the candidate cannot have them. Measured over the full boundary
    the record published 5.705 m and fired the warning; measured on the exterior rings the two
    perimeters are 1.476 m apart and nothing about the roof is in question.
    """
    from propx_roofs import imaging

    document = json.loads(ROOF_ATTRIBUTES.read_text(encoding="utf-8"))
    building = next(b for b in document["buildings"] if b["building_id"] == "vie-swv-007")
    authoritative = {
        "type": building["authoritative_roof_polygon"]["type"],
        "coordinates": building["authoritative_roof_polygon"]["coordinates"],
    }
    candidate = {
        "type": building["cv_candidate_polygon"]["type"],
        "coordinates": building["cv_candidate_polygon"]["coordinates"],
    }
    crop = imaging.build_crop(authoritative, CFG.study_area.cache_dir, CFG)
    result = agreement.compare(candidate, authoritative, crop, CFG)

    assert result.hausdorff_full_boundary_m == pytest.approx(5.705, abs=0.002)
    assert result.hausdorff_m == pytest.approx(1.476, abs=0.002)
    assert result.iou == pytest.approx(0.9216, abs=0.001)
    assert result.exterior_iou == pytest.approx(0.9246, abs=0.001)

    mismatch = result.topology_mismatch
    assert mismatch["flag"] is True
    assert mismatch["authoritative_interior_rings"] == 2
    assert mismatch["cv_interior_rings"] == 0
    assert mismatch["authoritative_interior_ring_areas_m2"] == [1.21, 0.69]
    assert mismatch["cv_interior_ring_areas_m2"] == []
    assert mismatch["crs"] == "EPSG:31256"

    # Both exterior figures sit comfortably inside the unchanged thresholds.
    assert result.hausdorff_m < WARN_HAUSDORFF_ABOVE_M
    assert result.exterior_iou > WARN_IOU_BELOW
    assert result.alignment_warning["flag"] is False
