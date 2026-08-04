"""GrabCut baseline and the agreement block.

The helpers come from ``test_imaging`` rather than a shared fixture module so that the synthetic
crop is defined exactly once; pytest puts the tests directory on ``sys.path``.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
from test_imaging import CFG, override, square_wgs84, synthetic_crop

from propx_roofs.segment import agreement, grabcut


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
    note = warning["note"]
    assert "does not indicate that the authoritative data is incorrect or outdated" in note
    quiet = agreement.alignment_warning(iou=0.95, hausdorff_m=0.4, sym_ratio=0.05, cfg=CFG)
    assert quiet == {"flag": False, "metrics": None, "note": None}
