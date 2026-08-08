# Study-Area Selection — Phase 2 Decision

> **Document status: HISTORICAL.** This is the Phase 2 decision record, kept verbatim as work
> history. Where it describes detector behaviour it describes the **Phase 2B detector, which has
> since been replaced** by the gated ridge detector (shadow exclusion, medial-axis crest test,
> shadow-flank and axis-alignment gates). Statements here about individual buildings' ridges are
> superseded by `docs/phase3_visual_validation.md` and by the current pipeline output. Inline
> **[SUPERSEDED]** notes mark the known contradictions.

**Status: COMPLETE.** Study area selected and 10 building units pinned in
`configs/study_area.yaml`, after inspecting real z20 imagery.

**Selected area: `sonnwendviertel`** — bbox `16.3760, 48.1830, 16.3820, 48.1880`
(Sonnwendviertel / Wiedner Gürtel, Vienna 3rd / 4th / 10th districts).

Reconnaissance run 2026-08-03. Source: City of Vienna open data, CC BY 4.0,
`Datenquelle: Stadt Wien - data.wien.gv.at`.

Three kinds of statement appear below and are labelled throughout:
**[OBSERVED]** seen in the 2024 orthophoto · **[AUTHORITATIVE]** from an official layer ·
**[HYPOTHESIS]** a tentative explanation that has not been confirmed.

---

## 1. What was fetched

All three candidate areas at z20, 414–437 tiles each, mosaics 4608–4864 × 5888 px.
Effective ground resolution **0.0995 m/px** against a native orthophoto GSD of 0.15 m — so the
pixel grid is finer than the information content, as designed.

**[OBSERVED] The imagery was flown 19/20 March, 29 March and 12 April 2024 — early spring.**
This has two consequences that run in opposite directions, and both matter more than expected:

- **Helpful:** deciduous canopy is bare, so tree occlusion of roofs is much lower than a
  summer flight would give. Trees appear as thin bare crowns.
- **Harmful:** vegetation is dormant. Extensive sedum roofs read **reddish-brown**, not green.
  An RGB vegetation index (ExG / VARI) will therefore under-detect green roofs on exactly the
  buildings that have them. This is a stronger limitation on `green_roof` than the missing NIR
  band, and it must be stated in the README rather than discovered by a reviewer.
- Sun elevation is low, so shadows are long and courtyards are often fully black.

## 2. Criteria table — completed from visual evidence

| Criterion | Sonnwendviertel (**selected**) | Karlsplatz / TU | Seestadt (fallback) |
|---|---|---|---|
| **[OBSERVED]** Visible roof diversity | **Good.** Large modern flat roofs in the centre and south, Gründerzeit pitched tiled roofs along Wiedner Gürtel / Argentinierstraße / Schelleingasse, plus curved and stepped forms | Narrow. Almost all articulated pitched grey-metal and tiled roofs; very few flat | Moderate. Overwhelmingly flat modern roofs; little pitched |
| **[AUTHORITATIVE]** `DACHFORM` mix | 13 flat / 41 pitched (24 / 76%) | 16 / 133 (11 / 89%) | 27 / 22 (55 / 45%) |
| **[AUTHORITATIVE]** `SLOPE_MEAN` min / median / max | 1.8 / 10.2 / 77.0 | 2.2 / 28.4 / 66.6 | 1.6 / 4.7 / 81.1 |
| **[OBSERVED]** Solar-panel candidates | **Strong.** Multiple unmistakable module grids, e.g. OBJECTID 351705 (full roof ring) and 351704 (near-full roof) | Weak. A few small dark regular patches that could equally be glazing or skylights; nothing unambiguous | **Strong.** Several large arrays |
| **[OBSERVED]** Potentially vegetated roofs | **Moderate.** Reddish-brown substrate surfaces with green fringes on several blocks (358722, 242275) — consistent with dormant extensive green roof | Weak. Courtyard planters and terraces only | **Strong.** Clear green roof strips and terraces |
| **[OBSERVED]** Tree occlusion | **Low.** Bare crowns; one selected roof (257807) has a bare tree over its edge, kept deliberately | Low, but courtyards are heavily self-shadowed | Low |
| **[OBSERVED]** Deep shadow | Moderate. Long shadows from the station roof and towers across open ground; roof interiors mostly readable | **High** in the narrow Gründerzeit courtyards — large fully black areas | Moderate. Long tower shadows |
| **[OBSERVED]** Understandable building boundaries | **Good** away from the station. Outlines follow buildings, courtyard voids excluded | Good per street address, but roofs are highly articulated with wings, glass roofs and terraces | Mixed. Many large buildings carry **no** roof record at all |
| **[AUTHORITATIVE]** Typology / year-built coverage in bbox | 97 typology polygons, 22 info points | **1153 / 128** (richest) | **0 / 0** (none) |
| **[AUTHORITATIVE]** Join quality, interior subset | 29 of 31 at IoU ≥ 0.90 | 102 of 116 at IoU ≥ 0.90 | 21 of 31 at IoU ≥ 0.90 |
| **8–10 defensible units available** | **Yes — 29 interior candidates at IoU ≥ 0.94** | Yes, but they would nearly all be one roof class | Marginal |

### Why Sonnwendviertel, and what it costs

It is the only candidate that satisfies **every** required criterion at once: both roof classes
genuinely present, unambiguous PV, plausible vegetated roofs, low occlusion, workable shadow,
and authoritative typology present.

- **Karlsplatz / TU has the best authoritative coverage and the best joins** — 1153 typology
  polygons and 102/116 interior matches at IoU ≥ 0.90 — and it was the stronger candidate on
  the vector evidence alone. It was rejected on **visual** evidence: no unambiguous solar-panel
  example, no convincing vegetated roof, and very deep courtyard shadow. Two explicit criteria
  fail. Choosing it would have meant a prettier join table and two attributes with nothing real
  to detect.
- **Seestadt has the strongest PV and green-roof imagery** but zero typology and zero
  building-info coverage, the weakest joins, and visible active construction — many buildings
  post-date the 2023 surface model and carry no roof record. It stays the documented fallback.
- **The cost of choosing Sonnwendviertel** is thinner authoritative context: only **3 of 10**
  selected buildings have typology and **1 of 10** has a `BAUJAHR`. That is a real weakness of
  the sample and is recorded as such, not smoothed over. `GEBAEUDETYPOGD` and
  `GEBAEUDEINFOOGD` cover historic stock; most of the Sonnwendviertel is post-2010.

## 3. Join verification — geometry, not counts

Run with `python3 tools/verify_join.py`; full output in `outputs/recon/join_verification.json`
and `outputs/recon/<area>/join_records.json` (git-ignored; regenerable by re-running
`make recon` then `python3 tools/verify_join.py`, both of which need network access — neither
file is present in a fresh clone).

Method: for each 2025 roof record, find the FMZK parts that overlap it, group them by
`BW_GEB_ID`, take the best-matching dissolved unit, and measure IoU plus both containment
ratios in a locally isotropic metric frame. Records or units touching the bbox edge are
identified separately, because a WFS bbox query truncates units that extend outside it.

| | Sonnwendviertel | Karlsplatz / TU | Seestadt |
|---|---|---|---|
| roof records | 54 | 149 | 49 |
| units with a `BW_GEB_ID` | 53 | 149 | 46 |
| FMZK parts with **null** `BW_GEB_ID` | 105 | 22 | 23 |
| roof records clipped by bbox | 23 | 32 | 17 |
| matched unit truncated by bbox | 22 | 33 | 18 |
| **interior subset** (neither clipped) | 31 | 116 | 31 |
| interior IoU ≥ 0.99 | 0 | 1 | 0 |
| interior 0.95–0.99 | 24 | 63 | 20 |
| interior 0.90–0.95 | 5 | 38 | 1 |
| interior < 0.80 | 2 | 3 | 10 |
| interior median roof-inside-unit | 0.983 | 0.975 | 0.980 |
| interior median unit-inside-roof | 0.983 | 0.975 | 0.979 |

### Answers to the three count questions

**The equal counts at Karlsplatz (149 vs 149) are a coincidence, not evidence of a 1:1 join.**
32 roof records and 33 units are clipped by the bbox, so the two totals are composed of
different, partly non-overlapping sets that happen to sum alike. Counts cannot establish this
relationship; only geometry can.

**Seestadt 49 vs 46** is explained by three separate mechanisms, all confirmed in the geometry:
23 FMZK parts carry a null `BW_GEB_ID` and so form no unit at all; **2 units genuinely carry
more than one roof record** (`5729190` has 4, `5576077` has 2), which is a real 1:n case; and 1
unit has no roof record. Seestadt is therefore the one area where the join is *not* cleanly 1:1.

**Sonnwendviertel 54 vs 53** is bbox-edge effect plus one roof record with no overlapping unit
(105 parts have null `BW_GEB_ID` here). No unit carries more than one roof record.

### The relationship itself

**[OBSERVED, from geometry] The 2025 roof outline is the same building as the `BW_GEB_ID`
dissolve, but not the same polygon.** The vertex-identity found earlier on the 2022 layer does
**not** carry over: only 1 of 178 interior records across all three areas reaches IoU ≥ 0.99.
Instead both containment ratios sit at a median of ~0.98 **symmetrically** — each polygon
contains ~98% of the other. A truncation artefact would be asymmetric. So this is a small
mutual re-cut of the outline, of order 2% of area.

**[HYPOTHESIS]** The re-cut reflects the 2025 layer being derived from 2025 imagery and a 2023
surface model rather than directly from the FMZK linework, so eaves and parapets fall slightly
differently. Not confirmed; the practical consequence is only that boundary identity cannot be
used as the join rule, and best-IoU overlap must be used instead.

**High part counts are legitimate large buildings, not sentinel identifiers.** This was the
open worry from Phase 1 and it resolves positively:

- Karlsplatz unit `5519487` — **198** FMZK parts, 7,816 m² dissolved, matched by exactly one
  roof record at **IoU 0.99** (`04., Wiedner Hauptstraße 8`). 198 parts dissolve into one clean
  building.
- Sonnwendviertel unit `5325896` — 542 parts, 37,477 m², bbox-truncated, one roof record at IoU
  0.927. **[HYPOTHESIS]** this is the Hauptbahnhof complex, consistent with its size and
  position. Excluded from the sample regardless: it is truncated and not an ordinary building.
- Seestadt unit `5525819` — 67 parts, 3,304 m², one roof record at IoU 0.948.

**`BW_GEB_ID` is therefore validated as a building-unit key**, with the caveat that a
non-trivial number of parts carry no identifier at all (105 of 1616 in Sonnwendviertel).

## 4. Selected buildings

Ten units, all from the 29 interior candidates with IoU ≥ 0.94 — every one fully inside the
bbox with an untruncated matched unit, so no selected geometry is a query artefact.

| id | OBJECTID | BW_GEB_ID | m² (recon) | `DACHFORM` | slope | kWp | FMZK IoU | why |
|---|---|---|---|---|---|---|---|---|
| vie-swv-001 | 351705 | 5576530 | 2302 | Flachdach | 4.8° | 75 | 0.983 | **[OBSERVED]** dense PV grid across the whole roof ring; 238 m² courtyard void correctly excluded |
| vie-swv-002 | 358722 | 5613428 | 6235 | Flachdach | 3.7° | 166 | 0.986 | largest unit; **[OBSERVED]** reddish substrate across three wings — green-roof candidate |
| vie-swv-003 | 351704 | 5576529 | 1084 | Schraegdach | 7.9° | 32 | 0.986 | **[OBSERVED]** near-full PV coverage; *and* a threshold-disagreement case |
| vie-swv-004 | 308209 | 5487628 | 3339 | Schraegdach | 7.8° | 105 | 0.985 | curved organic roof; threshold rule would call this flat |
| vie-swv-005 | 346520 | 5554877 | 544 | Flachdach | 4.3° | 18 | 0.966 | simple low-complexity flat roof, control case; **[AUTHORITATIVE]** typology present |
| vie-swv-006 | 242275 | 5371594 | 593 | Flachdach | 3.5° | 17 | 0.968 | **[OBSERVED]** reddish/green substrate — second green-roof candidate |
| vie-swv-007 | 346007 | 5553938 | 597 | Schraegdach | 34.8° | 25 | 0.957 | Gründerzeit tiled pitched roof with clear ridge; **[AUTHORITATIVE]** Gründerzeit typology |
| vie-swv-008 | 358486 | 5611476 | 677 | Schraegdach | 42.9° | 13 | 0.966 | selected on a manual reading of a possible authoritative/image discrepancy; the implemented detector does **not** reproduce it — see §5 |
| vie-swv-009 | 358487 | 5611478 | 451 | Schraegdach | 38.9° | 5 | 0.953 | articulated multi-plane roof; only 102 of 451 m² classified suitable; `BAUJAHR` 1891 |
| vie-swv-010 | 257807 | 5398899 | 382 | Schraegdach | 12.0° | 9 | 0.967 | between the 10° and 15° slope thresholds specified for the DACHFORM fallback — **that fallback was never implemented**, so this is not a live abstain band; bare tree overhangs the roof edge |

**m² (recon)** is the reconnaissance area in the local cos(latitude) frame, carried here only to show the size spread of the sample; the published area is `roof_area_m2`, computed in EPSG:31256, and runs ~0.743% higher (see `docs/phase1_design.md` §2 and `configs/study_area.yaml`). **FMZK IoU** is the authoritative-roof-record vs `BW_GEB_ID`-dissolve overlap (`fmzk_crosscheck.iou` in the output, EPSG:31256). It is not the CV-vs-authoritative agreement IoU, which is published at `delineation.agreement_with_authoritative_geometry.iou`, and neither is an accuracy figure.

Spread: `DACHFORM` 4 flat / 6 pitched · `SLOPE_MEAN` 3.5°–42.9° · area 382–6235 m² (16×) ·
capacity 5–166 kWp · IoU 0.953–0.986 · 3 with typology, 1 with `BAUJAHR` · 2 with courtyard
interior rings · 2 threshold-disagreement cases · 1 case selected for the (unimplemented)
slope-fallback band.

Deliberately **excluded**: the Hauptbahnhof mega-unit (truncated, not an ordinary building);
OBJECTID 275649, a shadowed triangular wedge that reads as a canopy rather than a building;
OBJECTID 300412, a 72 m² sliver over rail tracks.

**Also excluded, with a consequence:** the two `SLOPE_MEAN > 60°` outliers in this bbox
(351701 at 77.0°, 358724 at 76.7°) did not survive the interior / IoU filter, so **the
`requires_visual_review` flag has no exemplar in the final sample.** The mechanism stays in the
pipeline but will be exercised by no selected building. Recorded rather than engineered around.

### Required README caveat, verbatim

> The 8–10 buildings form a purposive sample selected for attribute diversity, source
> availability, and visual interpretability. They are not statistically representative of
> Vienna's building stock.

## 5. vie-swv-008 (OBJECTID 358486) — manual reading vs algorithmic evidence

**[AUTHORITATIVE]** `DACHFORM = "Schraegdach"`, `SLOPE_MEAN = 42.92°`.

**[HYPOTHESIS — manual visual reading, not confirmed]** At ~10 cm/px the roof *appeared*
predominantly flat to me during reconnaissance: a large light-grey surface carrying HVAC
plant, skylight strips and a raised penthouse, with no 43° plane obviously covering the
footprint. This was recorded as an observation at the time and is retained here because it is
what drove the selection.

**[OBSERVED — implemented detector]** The ridge detector built in Phase 2B **does not support
that reading**. It reports a ridge at ≈65.2° with plane-brightness contrast 0.23, which is
evidence *for* the authoritative `Schraegdach`. The automatic conflict flag therefore **does
not fire** on this building.

> **[SUPERSEDED]** The Phase 3 visual audit assessed that ≈65.2° line as *questionable* — most
> likely the boundary between the bright deck and the façade band inside the outline, not a
> ridge between two roof planes — and the current gated detector **withholds** it (the
> medial-axis crest gate rejects it as boundary-parallel structure). The paragraph above is the
> Phase 2B state, retained as history; it is no longer evidence for the authoritative class.

The disagreement between a human reading and the algorithm is the interesting part and is kept
on the record rather than resolved by decree. What must not happen is retuning the detector or
the threshold until the earlier hypothesis reappears — that would be fitting the instrument to
a guess. Whether the manual reading or the detector is right is unresolved and needs Phase 3
inspection.

**[HYPOTHESIS]** — none of these is confirmed, and they are not mutually exclusive:

1. `SLOPE_MEAN` may be computed over the classified irradiation sub-areas only (331 m² of
   677 m² here), so small steep elements — penthouse flanks, parapets, a pitched neighbouring
   section — could dominate the mean.
2. The record's outline may include a portion of an adjacent steeply pitched building.
3. A surface-model artefact from the tall adjacent structure.

**Why it stays in the sample.** It is direct evidence that an authoritative field can conflict
with what the imagery shows, which is the central tension of a hybrid design. Two design
implications follow, both for Phase 3 and neither implemented yet:

- `roof_type` is authoritative but **not infallible**. Its 0.90 confidence cap should be reduced
  when image evidence conflicts, and the conflict should be visible in the record.
- This is a second, better-motivated trigger for `requires_visual_review` than a slope threshold:
  disagreement between an authoritative class and the image, rather than an unusual number.

## 6. Visual artefacts for review

Under `outputs/recon/_review/` (git-ignored; regenerable):

| file | what |
|---|---|
| `sonn_selection_sheet.png` | **the review artefact** — labelled crop of each of the 10 selected roofs, outlines including courtyard holes |
| `sonn_candidates_sheet.png` | all 29 interior candidates, for auditing what was *not* chosen |
| `closeup_pair.png` | vie-swv-001 (PV confirmation) beside vie-swv-008 (the §5 manual reading the detector does not reproduce) |
| `<area>_overlay_thumb.png`, `<area>_mosaic_thumb.png` | whole-area context for all three candidates |
| `sonn_pv_complex.png`, `sonn_mid_blocks.png`, `sonn_south_resid.png`, `karls_dense_blocks.png`, `karls_modern_flat.png`, `see_pv_green.png` | full-resolution crops behind the §2 judgements |
| `selection.json` | machine-readable record of the 10, as generated |

No pipeline-derived polygon or attribute was edited. Every outline drawn is the source geometry
reprojected to mosaic pixels; every attribute quoted is the raw source field value.

## 7. Open risks and questions

1. **Sparse authoritative context** — 3/10 typology, 1/10 `BAUJAHR`. `construction_epoch` and
   `year_built` will be `not_in_source` for most of the sample. Correct behaviour, thin result.
2. **Dormant vegetation (§1)** is the dominant limit on `green_roof`, ahead of the missing NIR
   band. The two green-roof candidates may not be detectable by an RGB index at all — in which
   case the honest output is `unknown`, and that should be reported as a finding, not hidden.
3. **`DACHFORM` can conflict with the imagery (§5).** *Design aligned* (Amendment B4): authoritative
   value, image evidence and a conflict flag are kept separately visible, with a conflict penalty
   labelled as a heuristic. **Outstanding:** implement and test the three-field output and the
   penalty. The conflict mechanism is tested **synthetically**, on constructed evidence that
   genuinely disagrees — not by requiring any particular real building to trigger it. The
   implemented ridge detector reports a ridge on vie-swv-008 at ≈65.2°, which *supports* the
   authoritative `Schraegdach` rather than contradicting it; §5's "appears predominantly flat"
   remains a manual visual hypothesis and has not been confirmed by the algorithm. Neither the
   evidence nor the threshold may be adjusted to force the flag on.
4. **Join rule is best-IoU overlap, not boundary identity** (§3). *Design aligned* (Amendment B1/B2).
   **Outstanding implementation requirement:** buffered FMZK fetch before dissolve, best-IoU
   matching, recorded containment ratios and `second_best_iou`, ambiguity detection, and 1:1 / 1:n
   cardinality handling that never collapses silently — all with tests.
5. **1:n joins exist** (Seestadt units with 2 and 4 roof records). None in the selected sample,
   so aggregation stays unexercised — but the code path must still refuse to average silently.
6. **No `requires_visual_review` exemplar** in the final sample (§4).
