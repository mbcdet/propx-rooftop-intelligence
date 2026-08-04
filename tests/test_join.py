"""Join and dissolve: the evidence must survive, including when it is inconvenient.

Offline, on synthetic polygons built in code. The point of these tests is not that IoU
arithmetic works — it is that the join refuses to resolve an ambiguity by guessing: an
unmatched record stays unmatched, two competing units are both reported, and two roof
records over one building are both kept.

Where a test is about a threshold's *behaviour*, it pins the thresholds it is testing rather
than reading production values, so the test states its own premise. Everything else uses the
real ``configs/pipeline.yaml`` via ``config.load()``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from shapely.geometry import box

from propx_roofs import config, join, units
from propx_roofs.types import JoinEvidence

# A small patch of the pinned study area. 0.001 deg of longitude is ~74 m here, so these are
# building-sized rather than city-sized, which is what the join actually sees.
FETCH_BBOX = (16.3750, 48.1820, 16.3830, 48.1890)
LAT_LO, LAT_HI = 48.1850, 48.1860


@pytest.fixture(scope="module")
def cfg() -> config.Config:
    return config.load()


def _cfg_with_join_thresholds(base: config.Config, **join_values: float) -> config.Config:
    """A config whose join thresholds are fixed by the test, leaving the rest untouched."""
    thresholds = dict(base.thresholds)
    thresholds["join"] = {**thresholds["join"], **join_values}
    return replace(base, thresholds=thresholds)


def _unit(bw_geb_id: int, lon_lo: float, lon_hi: float, *, parts: int = 1) -> units.DissolvedUnit:
    geometry = box(lon_lo, LAT_LO, lon_hi, LAT_HI)
    return units.DissolvedUnit(
        bw_geb_id=bw_geb_id,
        geometry=geometry,
        part_count=parts,
        touches_fetch_boundary=not box(*FETCH_BBOX).contains(geometry),
    )


def _feature(bw_geb_id: object, lon_lo: float, lon_hi: float, *, omit_id: bool = False) -> dict:
    properties: dict[str, object] = {"FMZK_ID": 4000000000 + int(lon_hi * 1e4)}
    if not omit_id:
        properties["BW_GEB_ID"] = bw_geb_id
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": box(lon_lo, LAT_LO, lon_hi, LAT_HI).__geo_interface__,
    }


def test_identical_geometry_gives_iou_one(cfg: config.Config) -> None:
    roof = box(16.3790, LAT_LO, 16.3800, LAT_HI)
    evidence = join.match_roof_record(
        roof, {5611476: _unit(5611476, 16.3790, 16.3800)}, cfg, fmzk_parts_null_id_in_area=0
    )

    assert evidence.matched_by == "best_geometric_overlap"
    assert evidence.cardinality == "1:1"
    assert evidence.bw_geb_id == 5611476
    assert evidence.iou == pytest.approx(1.0)
    assert evidence.containment_roof_in_unit == pytest.approx(1.0)
    assert evidence.containment_unit_in_roof == pytest.approx(1.0)
    assert evidence.second_best_iou is None and evidence.second_best_bw_geb_id is None
    assert evidence.ambiguous is False and evidence.ambiguity_reasons == ()
    assert evidence.aggregation_applied is False


def test_disjoint_geometry_is_unmatched_and_never_zero(cfg: config.Config) -> None:
    """No matching unit means no value. Not IoU 0.0, which reads as a measured disagreement."""
    roof = box(16.3790, LAT_LO, 16.3800, LAT_HI)
    far_away = {5611476: _unit(5611476, 16.3810, 16.3820)}

    evidence = join.match_roof_record(roof, far_away, cfg, fmzk_parts_null_id_in_area=7)

    assert evidence.cardinality == "unmatched"
    assert evidence.bw_geb_id is None
    assert evidence.iou is None
    assert evidence.containment_roof_in_unit is None
    assert evidence.containment_unit_in_roof is None
    assert evidence.ambiguous is True
    assert "no FMZK unit intersects" in evidence.ambiguity_reasons[0]
    # The null-identifier count is reported even when nothing matched.
    assert evidence.fmzk_parts_null_id_in_area == 7


def test_low_overlap_is_flagged_ambiguous_with_a_readable_reason(cfg: config.Config) -> None:
    """A roof sitting inside a much larger unit overlaps well but matches poorly."""
    tuned = _cfg_with_join_thresholds(cfg, min_iou=0.50, ambiguity_margin=0.70)
    roof = box(16.3795, 48.1853, 16.3798, 48.1856)
    big_unit = {5576530: _unit(5576530, 16.3790, 16.3810, parts=59)}

    evidence = join.match_roof_record(roof, big_unit, tuned, fmzk_parts_null_id_in_area=0)

    assert evidence.iou < 0.50
    assert evidence.ambiguous is True
    assert len(evidence.ambiguity_reasons) == 1
    assert "below the configured minimum overlap" in evidence.ambiguity_reasons[0]
    # The roof is fully inside the unit; the asymmetry is the evidence, so it must survive.
    assert evidence.containment_roof_in_unit == pytest.approx(1.0, abs=1e-6)
    assert evidence.containment_unit_in_roof < 0.5
    assert evidence.fmzk_parts_in_unit == 59


def test_close_second_best_is_flagged_even_when_the_best_passes(cfg: config.Config) -> None:
    """Two units each covering most of the roof: the winner is not evidence of anything."""
    tuned = _cfg_with_join_thresholds(cfg, min_iou=0.50, ambiguity_margin=0.70)
    roof = box(16.3790, LAT_LO, 16.3810, LAT_HI)
    competing = {
        5611476: _unit(5611476, 16.3790, 16.3801),
        5611478: _unit(5611478, 16.3799, 16.3810),
    }

    evidence = join.match_roof_record(roof, competing, tuned, fmzk_parts_null_id_in_area=0)

    assert evidence.iou >= 0.50, "this test isolates the second-best rule from the IoU floor"
    assert evidence.second_best_iou is not None
    assert evidence.second_best_bw_geb_id in competing
    assert evidence.second_best_bw_geb_id != evidence.bw_geb_id
    assert evidence.second_best_iou / evidence.iou > 0.70
    assert evidence.ambiguous is True
    assert len(evidence.ambiguity_reasons) == 1
    assert "compete for this roof" in evidence.ambiguity_reasons[0]


def test_two_roof_records_over_one_unit_are_preserved_as_1_to_n(cfg: config.Config) -> None:
    """Reconnaissance found real cases. Averaging them would publish a guess as a fact.

    Cardinality reads building unit -> roof records, so one unit carrying two records is
    1:n. Seestadt units 5729190 (4 records) and 5576077 (2) are the documented instances.
    """
    unit = {5729190: _unit(5729190, 16.3790, 16.3810, parts=4)}
    left = box(16.3790, LAT_LO, 16.3800, LAT_HI)
    right = box(16.3800, LAT_LO, 16.3810, LAT_HI)

    evidences = {
        351705: join.match_roof_record(left, unit, cfg, fmzk_parts_null_id_in_area=0),
        351706: join.match_roof_record(right, unit, cfg, fmzk_parts_null_id_in_area=0),
    }
    assert all(e.bw_geb_id == 5729190 for e in evidences.values())

    resolved = join.assign_cardinality(evidences)

    assert set(resolved) == {351705, 351706}
    for record_id, other_id in ((351705, 351706), (351706, 351705)):
        evidence = resolved[record_id]
        assert evidence.cardinality == "1:n"
        assert evidence.cardinality_basis == "building_unit_to_roof_records"
        assert evidence.competing_roof_record_ids == (other_id,)
        assert evidence.aggregation_applied is False
        assert evidence.ambiguous is True
        assert any("same building unit 5729190" in r for r in evidence.ambiguity_reasons)
        # The ambiguity is about the records, not about which unit this is.
        assert any("would invent a figure" in r for r in evidence.ambiguity_reasons)
        # The winning unit is still named: the relationship is reported, not erased.
        assert evidence.bw_geb_id == 5729190
        published = evidence.as_dict()
        assert published["cardinality"] == "1:n"
        assert published["cardinality_basis"] == "building_unit_to_roof_records"
        assert published["competing_roof_record_ids"] == [other_id]
        assert published["aggregation"] == {"applied": False}


def test_cardinality_orientation_is_pinned_and_reversed_notation_is_rejected() -> None:
    """Regression guard for an orientation bug that reached the codebase once already.

    'n:1' and '1:n' describe the same physical situation from opposite ends, so a reversed
    label does not crash anything — it silently inverts the meaning of every join in the
    published output. The only defence is to make the reversed notation unconstructable and
    to publish the orientation next to the value.
    """
    base = dict(
        matched_by="best_geometric_overlap",
        bw_geb_id=5729190,
        iou=0.9,
        containment_roof_in_unit=0.95,
        containment_unit_in_roof=0.94,
        second_best_iou=None,
        second_best_bw_geb_id=None,
        ambiguous=True,
        ambiguity_reasons=("shared unit",),
        fmzk_parts_in_unit=4,
        fmzk_parts_null_id_in_area=0,
        fmzk_fetch_buffer_m=150.0,
        unit_touches_fetch_boundary=False,
    )

    with pytest.raises(ValueError, match="building_unit_to_roof_records"):
        JoinEvidence(cardinality="n:1", competing_roof_record_ids=(2,), **base)

    # 1:n must name its competitors, otherwise the relationship is asserted but not evidenced.
    with pytest.raises(ValueError, match="competing roof record ids"):
        JoinEvidence(cardinality="1:n", **base)

    # Aggregation cannot be switched on through the contract.
    with pytest.raises(ValueError, match="never averaged"):
        JoinEvidence(
            cardinality="1:n", competing_roof_record_ids=(2,), aggregation_applied=True, **base
        )

    ok = JoinEvidence(cardinality="1:n", competing_roof_record_ids=(2,), **base)
    assert ok.as_dict()["cardinality_basis"] == "building_unit_to_roof_records"


def test_one_to_one_matches_are_left_alone_by_assign_cardinality(cfg: config.Config) -> None:
    unit_a = {5611476: _unit(5611476, 16.3790, 16.3800)}
    unit_b = {5611478: _unit(5611478, 16.3805, 16.3815)}
    evidences = {
        1: join.match_roof_record(
            box(16.3790, LAT_LO, 16.3800, LAT_HI), unit_a, cfg, fmzk_parts_null_id_in_area=0
        ),
        2: join.match_roof_record(
            box(16.3805, LAT_LO, 16.3815, LAT_HI), unit_b, cfg, fmzk_parts_null_id_in_area=0
        ),
    }

    resolved = join.assign_cardinality(evidences)

    assert [e.cardinality for e in resolved.values()] == ["1:1", "1:1"]
    assert all(e.competing_roof_record_ids == () for e in resolved.values())


def test_parts_without_a_bw_geb_id_are_excluded_and_counted() -> None:
    """105 of 1616 parts in the study area carry no identifier. Silence would misreport it."""
    features = [
        _feature(5576530, 16.3790, 16.3795),
        _feature(5576530, 16.3795, 16.3800),
        _feature(None, 16.3800, 16.3802),
        _feature(None, 16.3802, 16.3804, omit_id=True),
        _feature("not-an-identifier", 16.3804, 16.3806),
    ]

    dissolved, null_id_parts = units.dissolve_units(features, FETCH_BBOX)

    assert null_id_parts == 3
    assert set(dissolved) == {5576530}
    assert dissolved[5576530].part_count == 2
    # The two parts share an edge, so the dissolve is a single polygon spanning both.
    assert dissolved[5576530].geometry.bounds[0] == pytest.approx(16.3790)
    assert dissolved[5576530].geometry.bounds[2] == pytest.approx(16.3800)


def test_string_identifiers_dissolve_with_their_numeric_twins() -> None:
    """City layers change field types between editions (design section 1.2), so coerce."""
    features = [_feature(5576530, 16.3790, 16.3795), _feature("5576530", 16.3795, 16.3800)]

    dissolved, null_id_parts = units.dissolve_units(features, FETCH_BBOX)

    assert null_id_parts == 0
    assert set(dissolved) == {5576530}
    assert dissolved[5576530].part_count == 2


def test_a_unit_reaching_the_fetch_edge_is_flagged() -> None:
    """An IoU depressed by a truncated unit is a fact about the query, not about the data."""
    inside = _feature(1, 16.3790, 16.3800)
    crossing = _feature(2, 16.3820, 16.3840)  # extends past FETCH_BBOX max_lon 16.3830

    dissolved, _ = units.dissolve_units([inside, crossing], FETCH_BBOX)

    assert dissolved[1].touches_fetch_boundary is False
    assert dissolved[2].touches_fetch_boundary is True


def test_evidence_serialises_with_its_caveat(cfg: config.Config) -> None:
    """The published record must carry the note that this is a cross-check, not the outline."""
    evidence = join.match_roof_record(
        box(16.3790, LAT_LO, 16.3800, LAT_HI),
        {5611476: _unit(5611476, 16.3790, 16.3800)},
        cfg,
        fmzk_parts_null_id_in_area=0,
    )

    published = evidence.as_dict()

    assert published["matched_by"] == "best_geometric_overlap"
    assert published["aggregation"] == {"applied": False}
    assert published["fmzk_fetch_buffer_m"] == cfg.threshold("join", "fmzk_buffer_m")
    assert "documented heuristics" in published["note"]
