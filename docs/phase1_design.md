# Phase 1 — Design and Reasoning (revision 3, approved, + Amendments A and B)

**Status:** revision 3 approved. **Amendment A** applied 2026-08-03 — roof-record source moved to the
verified current 2025 layer. **Amendment B** applied 2026-08-03 after Phase 2 reconnaissance completed —
join rule, CRS separation, conflict handling and green-roof honesty aligned to the measured evidence.
No further design revision cycle. **Phase 2 reconnaissance is complete** and this document, together with
`docs/study_area_selection.md`, is the baseline for implementation.
**Source verification date:** 2026-08-03. Every endpoint and figure comes from a live call or a local
computation reproduced below.

### Amendment A — approved source change

| # | Change |
|---|---|
| A1 | Roof-record source → **`ogdwien:ANLAGENLEISTUNG2025OGD`** (verified, `numberMatched=191625`) |
| A2 | Where a valid 2025 record exists, **its outline is the canonical authoritative roof polygon** — slope and roof form refer to that exact geometry |
| A3 | FMZK dissolve by `BW_GEB_ID` demoted to **cross-check and fallback** for roofs absent from the 2025 layer |
| A4 | **`DACHFORM` is the authoritative baseline roof-type source**; `SLOPE_MEAN` thresholds become a documented fallback only |
| A5 | Vintage discussion replaced with the verified 2025 basis: **2023 laser-scan surface model, 7.5 cm, 2025 aerial imagery** |
| A6 | 2022 layer (`ANLAGENLEISTUNGOGD`) retained as documented fallback/comparison only |
| A7 | `boundary_discrepancy` → **`boundary_alignment_warning`**, cautious diagnostic; makes no claim that official data is wrong or outdated |
| A8 | `ogdwien:PVPOTENZIALE2025OGD` recorded as **optional enrichment**, not a dependency |
| A9 | New findings from verifying the 2025 layer: `YR` is **null**, superseded by `POT_ERTRAG_Y_MIN`/`MAX`; `DACHFORM` string is ASCII `"Schraegdach"` |

### Amendment B — post-reconnaissance alignment (approved)

Applied 2026-08-03 after the Phase 2 reconnaissance. See `docs/study_area_selection.md` for the evidence.

| # | Change |
|---|---|
| B1 | **Boundary identity abandoned as the join rule.** Measured on real geometry, only 1 of 178 interior records reaches IoU ≥ 0.99. FMZK is joined by **best geometric overlap** (§1.4) |
| B2 | Robust join policy documented: buffered FMZK fetch, best-IoU selection, containment ratios recorded, ambiguity detection via second-best candidate, no silent averaging (§1.4) |
| B3 | **All final metric areas and distances are computed in EPSG:31256.** The local cos(lat) scaling used in reconnaissance is diagnostic-only and may never produce `roof_area_m2` (§2) |
| B4 | Authoritative roof type, image evidence and a conflict flag are kept **separately visible**; any confidence reduction is labelled a heuristic (§5.1, §6.2) |
| B5 | The two green-roof candidates are **visual candidates only**, never ground truth. `unknown` or low confidence is an acceptable and expected output (§5.2) |

Unchanged and still in force: authoritative polygon is primary, CV candidate is separate evidence, manual
review is QA only and cannot mutate output.

---

## 0. Repository and input state

- The repository contains the pinned study-area configuration, offline source cache, implementation,
  tests, published outputs and validation evidence required to reproduce the submission.
- The assessment brief is not redistributed; its requirements are addressed by the README and
  `docs/design_and_reasoning.md`.

---

## 1. Data sources — verified

### 1.1 Baseline sources

| Purpose | Source | Identifier | Live evidence |
|---|---|---|---|
| Primary RGB imagery | **Orthofoto 2024 Wien** (WMTS) | `https://mapsneu.wien.gv.at/wmts/lb2024/farbe/google3857/{z}/{y}/{x}.jpeg` | capabilities fetched: layer `lb2024`, TMS `google3857_0-21`, `image/jpeg` |
| **Roof outline, roof form, mean slope, PV potential** | **PV-Potenzial / Anlagenleistung 2025** (WFS) | **`ogdwien:ANLAGENLEISTUNG2025OGD`** | `numberMatched=191625`; fields in §1.2 |
| Building units (cross-check + fallback) | **FMZK Gebäude** (WFS) | `ogdwien:FMZKGEBOGD` | `numberMatched=780618`; `FMZK_ID`, `BW_GEB_ID`, `F_KLASSE` |
| Typology + construction epoch | **Gebäudetypologie** (WFS) | `ogdwien:GEBAEUDETYPOGD` | `numberMatched=438840`; `BAUTYP_TXT`, `OBJ_STR_TXT="1884-1918"` |
| Year built, architect | **Gebäudeinfo** (WFS, points) | `ogdwien:GEBAEUDEINFOOGD` | `numberMatched=58255`; `BAUJAHR=1875`, `ARCHITEKT` |
| *Optional enrichment* | **PV-Potenziale 2025** (WFS) | `ogdwien:PVPOTENZIALE2025OGD` | `numberMatched=1700559`; `EIGNUNGSKLASSE=3`, `EIGNUNGSKLASSE_TXT="Gut"`, MultiPolygon with holes |
| *Fallback / comparison only* | PV-Potenzial 2022 | `ogdwien:ANLAGENLEISTUNGOGD` | `numberMatched=185261` |

All WFS layers from `https://data.wien.gv.at/daten/geo`.
**Licence, all sources:** CC BY 4.0; attribution verbatim `Datenquelle: Stadt Wien - data.wien.gv.at`.
Redistribution of a small cached subset permitted with attribution.

**Imagery:** 15 cm GSD; flights 19/20 March, 29 March, 12 April 2024; documented as a *true* orthophoto.
The true-ortho property is a precondition of the method — without it, footprint-aligned roof analysis would
be displaced by roughly building height × off-nadir angle.

### 1.2 `ANLAGENLEISTUNG2025OGD` — verified fields

Live sample (2026-08-03), `OBJECTID=192652`, `16., Steinbruchstraße 34`:

```
SLOPE_MEAN=33.40913773  DACHFORM="Schraegdach"  ANLAGENLEISTUNG=5
SCHLECHT=3  MITTEL=42  GUT=15  SEHRGUT=11
POT_ERTRAG_Y_MIN=3000  POT_ERTRAG_Y_MAX=5000   YR=null
STR="Steinbruchstraße"  ONR=34  ADRESSE="16., Steinbruchstraße 34"  BEZ=1160
URL_1="https://www.wien.gv.at/umwelt/solarpotenzial-kataster"
```

| Source field | Meaning | Unit | Normalised label |
|---|---|---|---|
| `DACHFORM` | roof form, authoritative | `"Flachdach"` / `"Schraegdach"` | `roof_type` driver (§5.1) |
| `SLOPE_MEAN` | mean roof slope | degrees | `mean_slope_deg` |
| `SCHLECHT` / `MITTEL` / `GUT` / `SEHRGUT` | irradiation-class sub-areas | **m²** | `poor` / `medium` / `good` / `very_good` |
| `ANLAGENLEISTUNG` | estimated PV capacity for the roof | kWp | `pv_potential_kwp` |
| `POT_ERTRAG_Y_MIN` / `_MAX` | potential annual yield range | kWh/yr | `pv_potential_annual_yield_kwh` |
| `YR` | annual irradiation | kWh/m²/yr | **null in the 2025 layer — not used** |
| `ADRESSE`, `STR`, `ONR`, `BEZ` | address | — | `address` |

Three notes from verification:

- **`YR` is null** in the 2025 layer where it was populated in 2022. The methodology page confirms potential
  annual generation in kWh was *newly added* in 2025, so `POT_ERTRAG_Y_MIN`/`MAX` supersede it. The pipeline
  reads the yield range and treats `YR` as `not_in_source`. Had we assumed field parity with 2022 this would
  have shipped as a silent null.
- **`DACHFORM` is ASCII** — `"Schraegdach"`, not `"Schrägdach"`. The mapping matches on the exact observed
  string, and any unrecognised value maps to `unknown` rather than being coerced.
- **`ONR` and `BEZ` are numeric here** (`34`, `1160`) where the 2022 layer returned strings (`"74"`, `"1140"`).
  Address parsing must not assume the type.

Source field names are preserved verbatim in the JSON, with English labels carried alongside as a documented
mapping, so any value traces back to the published dataset without guessing:

```json
"pv_suitability_area_m2": {
  "source_fields": {"SCHLECHT": 3, "MITTEL": 42, "GUT": 15, "SEHRGUT": 11},
  "labels":        {"SCHLECHT": "poor", "MITTEL": "medium", "GUT": "good", "SEHRGUT": "very_good"},
  "unit": "m2", "total_m2": 71,
  "note": "classified irradiation sub-areas; they cover only part of the roof outline"
}
```

### 1.3 Geometry is a whole-roof outline — demonstrated

Areas recomputed locally (shoelace on a local tangent-plane projection):

```
2025 layer, OBJECTID 192652: polygon  99.1 m2 | category sum 71 m2 | categories cover 71.7%
2022 layer, OBJECTID 35:     polygon 125.8 m2 | category sum 89 m2 | categories cover 70.8%
2022 layer, OBJECTID 36:     polygon 187.5 m2 | category sum 91 m2 | categories cover 48.5%
```

Consistent across both editions: classified sub-areas cover part of a **whole-roof** polygon. The
methodology page states the vector products hold *"die Strahlungsbereiche nach Kategorie in Quadratmeter und
die geschätzte Anlagenleistung pro Dach"* — categories in m², capacity per roof.

A stronger test, run on the 2022 layer over one ~50 × 35 m box on Minorgasse (1140) — **3** roof records
against **5** `FMZKGEBOGD` parts:

| Roof record | FMZK parts | `BW_GEB_ID` |
|---|---|---|
| OBJECTID 36 — Minorgasse 74 | `4000837541` + `4000837566` | 5284589 |
| OBJECTID 43207 — Minorgasse 76 | `4000837570` + `4000837583` | 5463513 |
| OBJECTID 18592 — Minorgasse 72 | `4000837572` | 5361974 |

The roof outlines were **vertex-identical to eight decimal places with the union of the FMZK parts sharing a
`BW_GEB_ID`**, including a ~2 m² annex bump on Minorgasse 76. Two conclusions: the roof record is the FMZK
footprint dissolved to building level, and **`BW_GEB_ID` is a valid building-unit key** — the city's own
cadastre groups parts exactly that way.

**Phase 2 result: the identity does not carry over to the 2025 layer.** Measured across all three candidate
areas (`tools/verify_join.py`), only **1 of 178** interior roof records reaches IoU ≥ 0.99 against its
`BW_GEB_ID` dissolve. Instead both containment ratios sit at a median of **~0.98 symmetrically** — each
polygon contains ~98% of the other. Truncation would be asymmetric, so this is a genuine mutual re-cut of
order 2% of area, consistent with the 2025 layer deriving from 2025 imagery and a 2023 surface model rather
than from the FMZK linework.

Two conclusions carried into the design:

- **Boundary identity is no longer the join rule** (§1.4). It remains a useful *reported statistic* — a value
  near 1.0 is informative — but nothing depends on it.
- **`BW_GEB_ID` is still validated as a building-unit key.** Karlsplatz unit `5519487` dissolves **198**
  FMZK parts into a single clean building matched by one roof record at IoU 0.99. High part counts are large
  real buildings, not sentinel identifiers. Caveat: a non-trivial share of parts carry **no** identifier
  (105 of 1616 in the study area), so the fallback path must handle null `BW_GEB_ID`.

### 1.4 Canonical roof polygon and join policy

**Canonical geometry.** Where a valid `ANLAGENLEISTUNG2025OGD` record exists, **its outline is the
`authoritative_roof_polygon`** — `DACHFORM`, `SLOPE_MEAN` and the PV figures are computed for exactly that
geometry, so attaching them to a differently-cut polygon would misattribute them. FMZK dissolved by
`BW_GEB_ID` is the **cross-check**, and the **fallback** for roofs absent from the 2025 layer.

**Join policy** — all thresholds are config values, and all are **documented heuristics, not calibrated
accuracy figures**:

1. **Buffered fetch.** FMZK parts are fetched for the study bbox **expanded by a configurable buffer**
   (default 150 m) *before* dissolving by `BW_GEB_ID`. Measured effect of not doing this: 22 of 54 matched
   units in the study area were truncated by the bbox, which depresses IoU for reasons that have nothing to
   do with the data. Buffering is the difference between measuring the join and measuring the query.
2. **Best-overlap selection.** For each roof record, take the `BW_GEB_ID` unit with the **highest IoU** among
   those that intersect it. Not boundary identity, not centroid containment.
3. **Record the evidence, not just the winner.** Every join emits `iou`, `containment_roof_in_unit`,
   `containment_unit_in_roof`, `second_best_iou`, and whether either geometry touches the fetch boundary.
   The two containment ratios are what distinguish a mutual re-cut (symmetric) from truncation (asymmetric) —
   that distinction is why §1.3 could be resolved at all.
4. **Ambiguity detection.** The match is flagged `ambiguous` when either
   (a) `best_iou` < a minimum-overlap threshold (default 0.50), or
   (b) `second_best_iou / best_iou` exceeds a margin (default 0.70) — two units compete for the same roof.
   An ambiguous match still reports its candidates; it does not silently pick one and move on.
5. **Genuine 1:n is preserved, never collapsed.** **Cardinality is always read from the `BW_GEB_ID`
   building unit to the authoritative roof records** — one unit with one record is `1:1`, one unit with
   several records is `1:n`, no match is `unmatched`. The orientation is published beside every value as
   `cardinality_basis: "building_unit_to_roof_records"`, and `"n:1"` is not a permitted value, because
   reversing the notation silently inverts the meaning of every join rather than failing. Reconnaissance
   found real 1:n cases — the building unit is known and unambiguous; what is unresolved is that several
   authoritative roof records describe that same unit, and collapsing them into one building value would
   invent a figure the sources do not agree on. Seestadt
   units `5729190` (4 roof records) and `5576077` (2). Where a building genuinely maps to multiple roof
   records, the pipeline **does not average or dissolve by default**. It records
   `cardinality: "1:n"`, lists every record, and emits the attribute as `ambiguous_multiple_records` unless
   an explicit, reasoned aggregation is configured — in which case
   `aggregation: {applied: true, n_records: k, method: "…", reason: "…"}` carries a concrete observation as
   the reason, not a default.
6. **Null identifiers.** FMZK parts with `BW_GEB_ID = null` cannot be dissolved into a unit and are excluded
   from the cross-check, with the count reported. They are not silently dropped.
7. **No matching record** → `availability: "not_in_source"`, value `null`. Not flat, not zero. Enforced in code.

`mean_slope_deg` is taken **directly from the single matching record** in the 1:1 case — no area weighting by
default. All ten selected buildings are 1:1 with IoU 0.953–0.986, so the 1:n path will not be exercised by
the submitted sample. It is still implemented and tested, because an unexercised path that silently averages
is worse than one that refuses.

### 1.5 Verified source basis (2025 edition)

From `https://www.wien.gv.at/umwelt/solarpotenzial-kataster-methodik`:

- Radiation modelled from a **laser-scan digital surface model (DOM) at 7.5 cm**, flown **2023**.
- **2025 aerial imagery** (true orthophoto, 7.5 cm) used for roof-element segmentation.
- Far-shading from the 1 m DGM; direct and diffuse radiation computed hourly across a year.
- Irradiation grouped into 3 classes; areas below 600 kWh/m²/yr excluded from capacity.
- Flat vs pitched distinguished by a slope analysis — the basis of `DACHFORM`.
- Capacity assumes 450 Wp per module, with reduction factors from case studies for edges, roof windows,
  superstructures and access ways.

**Temporal alignment is now good:** a 2023 surface model and 2025 imagery against our 2024 orthophoto, with
the underlying model at 7.5 cm — finer than our 15 cm tiles. The 2022 edition (2019 data, 25 cm) is kept only
as a documented comparison. A modest recency factor is still applied, since the roof record and the imagery
are not the same epoch.

### 1.6 Calibration anchor

The city's own pipeline — 7.5 cm true-orthophoto segmentation, **twice our resolution** — detected
*"in etwa 70 Prozent aller bereits bestehender Photovoltaik und Solarthermieanlagen"*. A published, citable
ceiling: if a professional 7.5 cm pipeline recovers ~70% of installations, our 15 cm detector must not be
presented as reliable and negative results must stay weak (§6.1).

Secondary implication: the cadastre *excluded* detected existing installations from the potential, so a roof
with existing PV may show reduced potential area. Noted, not modelled.

### 1.7 Not used

- **`PVPOTENZIALE2025OGD`** — 1,700,559 MultiPolygon features with `EIGNUNGSKLASSE` /
  `EIGNUNGSKLASSE_TXT`, geometry containing holes where obstructions were cut out. **Optional enrichment
  only.** Its `ID` field (`1197` in the sample) has no documented relationship to
  `ANLAGENLEISTUNG2025OGD.OBJECTID`, so a join would have to be spatial and would need verifying. Included in
  the baseline only if recon shows clear benefit.
- **DOM / DGM rasters** — would give true per-plane slope and aspect rather than one mean. Out of scope for a
  one-day build; named because it is the obvious next step, and because `mean_slope_deg` should not be
  implied to be the best obtainable.
- **Orthofoto 2025** (`lb`) exists. Staying on 2024 as agreed; the year is config.
- **OpenStreetMap** — `roof:shape` unevenly populated and unverifiable per building.
- `ogdwien:BAUKOERPEROGD`, `PVPOTENZIALOGD`, `PVPOTENZIALFLOGD` — service exception, do not exist.

---

## 2. Resolution, CRS, dependencies

Web Mercator resolution = `156543.0339 · cos(φ) / 2^z`. At φ = 48.2°:

| zoom | m/px | vs. native 15 cm |
|---|---|---|
| 19 | 0.199 | undersamples |
| **20** | **0.0995** | oversamples ≈1.5× |
| 21 | 0.0498 | oversamples ≈3× |

**z20** — smallest zoom discarding no native detail. The README must state that effective information
content stays ~15 cm regardless of a 10 cm pixel grid.

- Tiles and mosaic: **EPSG:3857** (the actual tile grid).
- All areas, distances, azimuths: **EPSG:31256** (MGI / Austria GK East). Not 3857 — Web Mercator area error
  at Vienna's latitude is ≈2.2× and would silently corrupt every `roof_area_m2`.

**Two coordinate frames exist and must not be confused (B3).** Reconnaissance used a cheap local
`cos(latitude)` scaling of degrees to compare geometries, because IoU is a ratio and only needs local
isotropy — `pyproj` was not required for that. That frame is **diagnostic only**:

| purpose | frame | may produce |
|---|---|---|
| IoU, containment ratios, join diagnostics | local cos(lat) scaling (recon) **or** EPSG:31256 | comparison evidence only |
| `roof_area_m2`, distances, azimuths, every published metric | **EPSG:31256 via `pyproj`, exclusively** | final output values |

**Measured, not estimated:** the local scaling carries **≈0.743% area error** at this latitude — identical
for a 30 m square and for the full study bbox. The cause is not the small-angle approximation but a wrong
constant: `M_PER_DEG_LAT = 110574.0` is the *equatorial* meridian-arc length, where the value at 48.19° is
≈111,200 m. Negligible for a ratio, not acceptable in a published area. So the geometry module exposes the
projected transform as the only route to a metric value, `roof_area_m2` is computed solely from it, and a
test asserts that a known polygon's EPSG:31256 area differs from its locally-scaled area, so the two can
never be quietly interchanged.

The same 0.743% appears as a systematic offset between `configs/study_area.yaml`'s reconnaissance
diagnostic areas and pipeline output (e.g. vie-swv-001: 2302.3 m² diagnostic vs 2319.5 m² in EPSG:31256).
The config field is named `recon_diagnostic_area_m2` so it cannot be mistaken for a published area.

**Dependencies (approved):** `requests`, `shapely`, `pyproj`, `numpy`, `opencv-python-headless`, `Pillow`,
`jsonschema`, `PyYAML`. No GDAL / rasterio / geopandas — the tile grid is analytic, so the mosaic transform is
~20 lines plus a JSON sidecar. Scoped to this fixed WMTS/WFS pipeline.

### 2.1 A silent WFS failure mode — worth a regression test

Requesting a small bbox with `version=1.1.0` and a bare `bbox` (no CRS suffix) returned **HTTP 200 with an
empty FeatureCollection in *both* coordinate orders**:

```
version=1.1.0  bbox=48.20130,16.26930,48.20175,16.26995                            → numberMatched=0
version=1.1.0  bbox=16.26930,48.20130,16.26995,48.20175                            → numberMatched=0
version=2.0.0  bbox=48.20130,…,urn:ogc:def:crs:EPSG::4326                          → numberMatched=3 ✓
```

An empty result with a 200 is the worst failure mode: it reads as "no buildings here" rather than a malformed
request. The pipeline therefore **always** uses `version=2.0.0` with an explicit
`urn:ogc:def:crs:EPSG::4326` suffix and lat,lon order, and a test asserts a known-populated bbox returns a
non-zero count.

---

## 3. Building units and study area

### 3.1 Building units

`FMZKGEBOGD` polygons are building *parts* — courtyard wings, stair cores, annexes (5 parts for 3 buildings
in §1.3). A building unit is `FMZKGEBOGD` parts dissolved by `BW_GEB_ID`, used as cross-check and fallback
per §1.4. Chosen unit IDs and roof-record `OBJECTID`s are pinned in `configs/study_area.yaml` with the fetch
date, so runs stay stable if the city republishes.

### 3.2 Roof outline

`authoritative_roof_polygon` = the `ANLAGENLEISTUNG2025OGD` outline where a valid record exists; otherwise
the `BW_GEB_ID`-dissolved FMZK unit, with `polygon_source` naming which was used.

### 3.3 Study-area reconnaissance — complete; area selected

**Phase 2 decision (2026-08-03): the study area is `sonnwendviertel`** — bbox
`16.3760, 48.1830, 16.3820, 48.1880` — with **10 building units pinned** in `configs/study_area.yaml`.
Full evidence, criteria table and rationale in `docs/study_area_selection.md`.

Candidates assessed were **Sonnwendviertel / Favoriten** (selected), **Karlsplatz / TU Wien** and
**Seestadt Aspern** (retained as documented fallback). The decision rested on inspected z20 pixels, not
district reputation: Karlsplatz had the richest authoritative coverage and the best joins but no unambiguous
solar-panel example, no convincing vegetated roof and very deep courtyard shadow, so two required criteria
failed. The procedure that produced the decision was:

1. Fetch real z20 `lb2024` tiles per candidate bbox; build the mosaic; save a PNG per area.
2. Query `ANLAGENLEISTUNG2025OGD`, FMZK, typology and building info per bbox with WFS 2.0 + explicit CRS.
3. For each roof record, match the best-overlapping `BW_GEB_ID` unit and record the evidence — `iou`, both
   containment ratios, `second_best_iou`, bbox-edge contact, and the resulting cardinality — together with
   whether `DACHFORM`, `SLOPE_MEAN`, typology and `BAUJAHR` are present.
   **Outcome (`tools/verify_join.py`, 2026-08-03):** identity was tested and rejected — 1 of 178 interior
   records at IoU ≥ 0.99, with both containment ratios symmetric at ~0.98. The evidence policy of §1.4
   (best-IoU plus containment ratios, ambiguity detection, no silent collapsing) is what the pipeline uses.
4. Score each area on the agreed criteria, recorded in `docs/study_area_selection.md` with the mosaics:

| Criterion | How judged |
|---|---|
| Visible roof diversity | visual — flat and pitched both present |
| Authoritative roof-form and slope diversity | mix of `DACHFORM` values and spread of `SLOPE_MEAN` after a valid join |
| Solar-panel candidate | ≥1, visual |
| Green-roof candidate | ≥1, visual |
| Limited shadow / tree occlusion | visual, over roof interiors specifically |
| Compactness | ≤ ~600 m box |
| Understandable building units | units a human would call buildings |
| Reliable per-building joins | each selected unit resolves to exactly one roof record |

5. Pick the area, then pin 8–10 units, every one inspected before entering the config.
   **Outcome:** 10 units pinned from 29 interior candidates at IoU 0.953–0.986 — 4 `Flachdach` / 6
   `Schraegdach`, `SLOPE_MEAN` 3.5°–42.9°, area 382–6235 m², 5–166 kWp, including two PV exemplars, two
   green-roof *candidates* (§5.2), two slope-threshold disagreement cases, one abstain-band case and one
   authoritative-versus-image conflict case (§5.1).

### 3.4 Required README caveat, verbatim

> The 8–10 buildings form a purposive sample selected for attribute diversity, source availability, and
> visual interpretability. They are not statistically representative of Vienna's building stock.

---

## 4. Delineation — a hybrid with no ambiguity about who did what

**Authoritative geodata provides delineation and geolocation. Imagery and CV provide visible-attribute
extraction, consistency evidence, and alignment diagnostics.** Nothing in the output may imply the
authoritative polygon was produced by CV.

- **`authoritative_roof_polygon`** — primary outline, from official geodata (§3.2). Always primary.
- **`cv_candidate_polygon`** — GrabCut + geometric-refinement result. Comparison evidence, never substituted
  for the primary outline. `null` on failure, which is not an error.
- **No promotion mechanism.** The deterministic pipeline does not change its primary polygon on the basis of
  any reviewer entry, and manual review cannot mutate output (§7).

### 4.1 Comparison evidence

The CV polygon is seeded from the authoritative outline, so overlap measures **agreement between two related
estimates**, not correctness. Named accordingly, never labelled accuracy:

```
agreement_with_authoritative_geometry: {
  iou, symmetric_difference_ratio, hausdorff_m,
  boundary_gradient_ratio     # mean image gradient along CV boundary ÷ along authoritative boundary
}
```

`boundary_gradient_ratio` is the one signal independent of the seed. It is reported as evidence and decides
nothing about the primary polygon.

### 4.2 `boundary_alignment_warning` — a cautious diagnostic

Where the CV boundary diverges materially from the authoritative outline, the record carries
`boundary_alignment_warning: {flag, metrics, note}`. It is a **statement about our own agreement measurement**,
not about the official data:

> "The image-derived candidate boundary differs materially from the authoritative outline in this area.
> Possible causes include segmentation error, shadow or occlusion, roof overhang, or a difference in epoch
> between imagery and the roof record. This is a flag for human inspection and does not indicate that the
> authoritative data is incorrect or outdated."

If recon shows the flag fires mostly on segmentation noise and adds no interpretive value, it is dropped
rather than kept for appearances.

### 4.3 Segmenter

Baseline: **footprint-seeded GrabCut plus geometric refinement.** The authoritative outline is eroded to a
sure-roof mask and dilated to a sure-background ring, forming a trimap; GrabCut segments within it; the
boundary is regularised against dominant image gradients. OpenCV only, deterministic, sub-second per
building, reproducible offline from the committed cache.

**SAM is a post-baseline stretch item only** — after the complete baseline runs and is visually validated,
behind an opt-in flag, **never a required dependency**. No SAM weights in the install path, the Dockerfile,
or `make run`. Training or fine-tuning a CNN is rejected: not honestly doable in a day.

---

## 5. Attributes

Provenance vocabulary: `observed` (measured from imagery) · `derived` (computed from geometry) · `inferred`
(classified, with uncertainty) · `authoritative` (official source) · `unavailable` (not obtainable from any
source used) · `not_in_source` (source used, record absent).

| Attribute | Method | Provenance | Cap | Notes |
|---|---|---|---|---|
| `authoritative_roof_polygon` | §3.2 | `authoritative` | 0.90 | always primary; `polygon_source` names the layer |
| `cv_candidate_polygon` + `agreement_…` | §4.1 | `observed` | — | evidence, not an attribute value |
| `roof_area_m2` | area of the authoritative outline in EPSG:31256 | `derived` | 0.85 | **planimetric**, stated in-record |
| `roof_type` ∈ `flat`/`pitched`/`complex`/`unknown` | **`DACHFORM`** (§5.1) | `authoritative` | 0.90 | slope thresholds are fallback only; authoritative value, image evidence and conflict flag stay separate (§5.1) |
| `roof_subtype` ∈ `gable`/`hip`/`null` | only on strong ridge evidence | `inferred` | 0.60 | `null` default; absence is not a claim |
| `roof_surface_class` ∈ `tiled`/`bitumen_gravel`/`metal`/`vegetated`/`mixed`/`unknown` | RGB colour + texture | `inferred` | 0.60 | "visible surface class" — RGB cannot prove construction material |
| `ridge_orientation_deg` | azimuth of the detected ridge line | `observed` | 0.70 | **only when a ridge is detected**; otherwise absent |
| `footprint_axis_orientation_deg` | long axis of the minimum rotated rectangle | `derived` | 0.80 | building/roof long axis; **not** a slope aspect |
| `mean_slope_deg` | `SLOPE_MEAN` from the matched record | `authoritative` | 0.90 | mean over the whole roof, not per plane |
| `pv_potential_kwp` | `ANLAGENLEISTUNG` | `authoritative` | 0.90 | modelled potential, never installed capacity |
| `pv_potential_annual_yield_kwh` | `POT_ERTRAG_Y_MIN`–`MAX` | `authoritative` | 0.90 | emitted as a **range**, not a point value |
| `pv_suitability_area_m2` | four category fields (§1.2) | `authoritative` | 0.90 | m², source names preserved, sub-areas of the roof |
| `solar_panels` | dark, low-saturation, regularly spaced rectangular clusters | `observed` | 0.75 pos / 0.45 neg | asymmetric (§6.1); ceiling anchored by §1.6 |
| `green_roof` (+ vegetated fraction) | RGB vegetation index (ExG / VARI) | `observed` | 0.65 pos / 0.45 neg | **no NIR**; **dormant vegetation in the March/April imagery is the larger limit** — `unknown` is an accepted output (§5.2) |
| `construction_epoch`, `building_typology` | `GEBAEUDETYPOGD` | `authoritative` | 0.95 | context; **not** a roof-type driver |
| `year_built`, `architect` | `GEBAEUDEINFOOGD` | `authoritative` | 0.95 | absent → `not_in_source` |
| `address` | roof record `ADRESSE` | `authoritative` | 0.95 | type-tolerant parsing (§1.2) |

**Dropped:** `roof_surface_area_m2` (planimetric ÷ cos of a whole-roof mean slope is not defensible on a
multi-plane roof), `roof_obstructions` (not solid at 15 cm on JPEG), and `annual_irradiation_kwh_m2` (`YR` is
null in the 2025 layer).

### 5.1 Roof type comes from `DACHFORM`

Primary mapping, on the exact observed string:

- `"Flachdach"` → `flat`
- `"Schraegdach"` → `pitched`
- any other or missing value → `unknown` (never coerced)
- `complex` — only when **additional** geometric or image evidence supports it: multiple inconsistent ridge
  directions, high boundary complexity, or multi-modal plane brightness. Never from `DACHFORM` alone, which
  is binary.

**Documented fallback**, used only where no 2025 record exists: `mean_slope_deg` thresholds `< ~10°` → `flat`,
`> ~15°` → `pitched`, `10–15°` → `unknown`. The abstention band exists because a mean over a mansard or
multi-plane roof can land mid-range with no flat plane on it. When the fallback is used the record says so via
`method`.

`construction_epoch` is **context only** — not a prior, and it does not shift the category. "1884-1918,
therefore pitched" is exactly the shortcut that produces a confident wrong answer on a converted roof.

**`DACHFORM` is authoritative but not infallible (B4).** Reconnaissance found a concrete counter-example in
the selected sample: **vie-swv-008 / OBJECTID 358486** carries `DACHFORM = "Schraegdach"` and
`SLOPE_MEAN = 42.92°`, while at ~10 cm/px the roof reads as predominantly flat — a large light-grey surface
with HVAC plant, skylight strips and a raised penthouse. Three unconfirmed explanations are recorded in
`docs/study_area_selection.md` §5; none is asserted.

The output design keeps the three things **separately visible** and never resolves them into one number:

| field | content |
|---|---|
| `roof_type.value` + `source_detail.raw_value` | the **authoritative** class, unmodified. Never overridden by our image evidence |
| `roof_type.image_evidence` | what the imagery indicates, as an independent observation with its own method and rationale |
| `roof_type.conflict` | `{flag, description, status: "requires_visual_review"}` when the two disagree |

**The authoritative value is not silently corrected, and the image value is not promoted.** A reader sees
both and the flag. Confidence is reduced when they conflict, and that reduction is labelled a heuristic
(§6.2) — not a probability that the authoritative source is wrong.

**The flag fires only when the implemented evidence rule genuinely disagrees — never to satisfy an
expectation.** The visual reading above is a *manual hypothesis*, and the implemented ridge detector does
not currently support it: it reports a ridge on vie-swv-008 at ≈65.2° with plane-brightness contrast 0.23,
which is evidence *for* `Schraegdach`. On that evidence the conflict flag stays off for this building and
the authoritative value publishes unmodified. Retuning the detector or the threshold to reproduce the
earlier hypothesis would be fitting the instrument to a guess. The conflict mechanism is therefore tested
**synthetically**, on constructed evidence that genuinely disagrees, so its correctness never depends on
any particular real building behaving a particular way.

The other honest limitation on `roof_type` is **epoch mismatch**: a roof rebuilt between the 2023 model
inputs and the 2024 imagery.

### 5.2 `green_roof`: visual candidates are not ground truth (B5)

Reconnaissance identified two **visual candidates** — vie-swv-002 (358722) and vie-swv-006 (242275) — whose
roof surfaces read as reddish-brown substrate with green fringes. That is all they are: candidates.

They are **not** recorded as ground-truth positives anywhere in the repo, they do not enter any threshold
calibration, and the pipeline is not tuned to make them come out `true`. Doing so would be fitting the
detector to a handful of eyeballed guesses and reporting the result as detection.

Two source-side reasons make honest detection hard, and the second is the larger:

1. No NIR band, so no NDVI — only weaker RGB indices (ExG / VARI).
2. **The imagery was flown 19/20 March, 29 March and 12 April 2024.** Vegetation is dormant, so extensive
   sedum roofs appear reddish-brown rather than green. An RGB vegetation index will therefore under-detect
   green roofs on exactly the buildings that have them. (The same early-spring flight *helps* elsewhere:
   deciduous canopy is bare, so tree occlusion is unusually low.)

**Accepted outcome:** `green_roof` may honestly return `unknown` or a low-confidence `false` on both
candidates. If it does, that is reported as a finding about the source and the season — not hidden, not
worked around by loosening a threshold until the expected answer appears.

---

## 6. Confidence model

`score = base(provenance) × Π(penalty_i)`, clipped to [0, 1], **at most three** penalty factors per attribute,
each in [0.5, 1.0], all recorded by name.

- Base: `authoritative` 0.95 · `derived` 0.85 · `observed` 0.80 · `inferred` 0.65.
- Penalties drawn from: agreement evidence (§4.1) · shadow fraction over the roof interior · distance from the
  classification threshold · feature-size-vs-resolution adequacy · source recency (§1.5).

Each attribute emits `{value, unit, availability, confidence: {score, method, sources[], rationale,
limitations[], factors{}}}`.

Stated in the README, this document and the JSON: these are **documented heuristic reliability indicators,
not calibrated probabilities**, not validated against labelled ground truth. Abstention is the cheap path in
code, so it is the default rather than an exception.

### 6.1 Positive and negative detections are asymmetric

Absence of evidence is weak evidence of absence — shadow, canopy, JPEG artefacts and a 15 cm limit all
suppress detection, and §1.6 shows even a 7.5 cm professional pipeline recovers only ~70% of installations.

- Strong, regular positive evidence → `true`, up to the positive cap.
- Nothing detected, image quality over the roof good → `false`, **capped at 0.45**, rationale naming
  absence-of-evidence explicitly.
- Nothing detected but the roof substantially shadowed or occluded → **`unknown`**, not `false`.

Caps and the shadow threshold are config; a unit test asserts no negative detection exceeds its cap.

### 6.2 The conflict penalty is a heuristic, and says so

When an authoritative class and the image evidence disagree (§5.1), confidence in the authoritative attribute
is reduced by a configured factor. The record states plainly what that means:

> "Confidence reduced by a documented heuristic factor because the image evidence disagrees with the
> authoritative class. This is not an estimate of the probability that the authoritative source is wrong; it
> flags a disagreement for human review."

The authoritative value itself is unchanged, the image observation is reported separately, and the flag is
visible. Three things a reader can weigh independently — rather than one blended number that hides which
source lost.

Join diagnostics feed confidence the same way: the §1.4 thresholds (minimum overlap 0.50, ambiguity margin
0.70) are documented heuristics chosen to be conservative, not calibrated accuracy values, and the README
says so in those words.

---

## 7. Manual review — QA only, and it changes nothing — **ORIGINAL PROPOSAL, NOT THE BUILT REPOSITORY**

> **Historical record.** The CSV review loop described below was **never built**. There is no
> `make review-template` and no `make review-report` target, and neither
> `outputs/manual_review_template.csv` nor `outputs/manual_review.csv` exists — consistent with §9,
> which records the same targets as dropped. What was built instead is the Phase 3 / Phase 4A
> reviewer pass recorded in `docs/phase3_visual_validation.md` and `validation/visual_audit.json`,
> which is likewise read-only with respect to `roof_attributes.json`. The validation artifact is
> model-assisted QA evidence, **not human validation**; independent human review would still be
> required before operational use.
> The principle stated in this section — review is QA and mutates nothing — did survive: no review
> input is read by any code path, and `run.manual_review` is fixed at
> `{"status": "not_yet_reviewed", "reviewer": null, "n": null}`, asserted in
> `tests/test_provenance.py` and `tests/test_pipeline.py`.

The proposed human-review workflow inspects all selected buildings. Review is **validation and QA**; it
**cannot mutate, promote, or override any deterministic pipeline output**. Re-running with or without a
filled review file produces byte-identical `roof_attributes.json`, and a test asserts this.

- `make review-template` writes `outputs/manual_review_template.csv`, one row per building with the overlay
  filename and **empty** reviewer columns: `reviewer_roof_type`, `reviewer_roof_subtype`,
  `reviewer_surface_class`, `reviewer_solar_panels`, `reviewer_green_roof`,
  `reviewer_polygon_looks_correct`, `notes`, plus `reviewer` and `review_date`.
- **No labels pre-populated, suggested, or inferred.** A test asserts reviewer columns are blank on generation.
- Filled as `outputs/manual_review.csv`, `make review-report` writes a **separate** file comparing his labels
  to the pipeline's categorical outputs, marked **`n=8–10 manual spot check, not a validation metric`**. It
  never writes back into `roof_attributes.json`.
- Absent or incomplete → report and README state "not yet reviewed". No number without his labels behind it.

---

## 8. Output schema (sketch)

```json
{
  "run": {
    "generated_at": "…", "pipeline_version": "…", "config_hash": "…",
    "study_area": {"name": "…", "bbox_wgs84": [], "crs_metric": "EPSG:31256", "imagery_zoom": 20},
    "sources": [
      {"name": "Orthofoto 2024 Wien", "url": "…", "year": 2024, "resolution_m": 0.15,
       "licence": "CC BY 4.0", "attribution": "Datenquelle: Stadt Wien - data.wien.gv.at", "accessed": "2026-…"},
      {"name": "PV-Potenzial 2025 (ogdwien:ANLAGENLEISTUNG2025OGD)", "url": "…",
       "model_basis": "2023 laser-scan surface model (DOM) at 7.5 cm; 2025 aerial imagery; 1 m DGM for far shading",
       "licence": "CC BY 4.0", "attribution": "Datenquelle: Stadt Wien - data.wien.gv.at", "accessed": "2026-…"}
    ],
    "confidence_note": "Heuristic reliability indicators, not calibrated probabilities. Not validated against labelled ground truth.",
    "sample_note": "Purposive sample selected for attribute diversity, source availability and visual interpretability; not statistically representative.",
    "manual_review": {"status": "not_yet_reviewed", "reviewer": null, "n": null}
  },
  "buildings": [{
    "building_id": "vie-KP-001",
    "source_ids": {"anlagenleistung2025_objectid": 192652, "bw_geb_id": 5284589,
                   "fmzk_ids": [4000837541, 4000837566]},
    "authoritative_roof_polygon": {"type": "Polygon", "coordinates": [], "crs": "EPSG:4326",
                                   "polygon_source": "ogdwien:ANLAGENLEISTUNG2025OGD"},
    "cv_candidate_polygon": {"type": "Polygon", "coordinates": [], "crs": "EPSG:4326"},
    "delineation": {
      "primary_polygon": "authoritative_roof_polygon",
      "segmenter": "grabcut+geometric_refinement",
      "agreement_with_authoritative_geometry": {
        "iou": 0.87, "symmetric_difference_ratio": 0.14, "hausdorff_m": 1.4,
        "boundary_gradient_ratio": 0.92},
      "note": "Comparison evidence only. The CV polygon is seeded from the authoritative outline; this is agreement between related estimates, not segmentation accuracy. The primary outline is authoritative geodata, not a CV product.",
      "boundary_alignment_warning": {"flag": false, "metrics": null, "note": null}
    },
    "fmzk_crosscheck": {
      "matched_by": "best_geometric_overlap",
      "cardinality": "1:1",
      "cardinality_basis": "building_unit_to_roof_records",
      "bw_geb_id": 5611476,
      "iou": 0.967,
      "containment_roof_in_unit": 0.983,
      "containment_unit_in_roof": 0.981,
      "second_best_iou": 0.041,
      "ambiguous": false,
      "fmzk_fetch_buffer_m": 150,
      "either_geometry_touches_fetch_boundary": false,
      "aggregation": {"applied": false},
      "note": "Cross-check only. The canonical outline is the 2025 roof record. Thresholds are documented heuristics, not calibrated accuracy values."
    },
    "attributes": {
      "roof_type": {"value": "pitched", "availability": "authoritative",
        "source_detail": {"layer": "ogdwien:ANLAGENLEISTUNG2025OGD", "field": "DACHFORM",
                          "raw_value": "Schraegdach"},
        "image_evidence": {"indicates": "flat", "method": "plane_brightness_and_ridge_analysis",
                           "rationale": "single dominant plane, no ridge detected"},
        "conflict": {"flag": true, "status": "requires_visual_review",
          "description": "Authoritative DACHFORM indicates pitched; image evidence indicates a predominantly flat roof. The authoritative value is reported unchanged and has not been overridden."},
        "confidence": {"score": 0.63, "method": "authoritative_dachform",
          "sources": ["PV-Potenzial 2025 (ogdwien:ANLAGENLEISTUNG2025OGD)"],
          "rationale": "authoritative roof form for this exact outline; reduced because image evidence disagrees",
          "limitations": ["binary flat/pitched; cannot express gable, hip or mansard",
                          "roof may have changed since the 2023 model inputs",
                          "conflict factor is a documented heuristic, not a probability that the source is wrong"],
          "factors": {"source_recency": 0.95, "image_conflict": 0.70}}},
      "mean_slope_deg": {"value": 33.41, "unit": "deg", "availability": "authoritative",
        "source_detail": {"layer": "ogdwien:ANLAGENLEISTUNG2025OGD", "field": "SLOPE_MEAN",
                          "records_matched": 1, "aggregated": false},
        "confidence": {"score": 0.86, "method": "authoritative_single_record",
          "sources": ["PV-Potenzial 2025 (ogdwien:ANLAGENLEISTUNG2025OGD)"],
          "rationale": "taken directly from the single matched roof record",
          "limitations": ["mean over the whole roof, not per plane"],
          "factors": {"source_recency": 0.95}}},
      "solar_panels": {"value": false, "availability": "observed",
        "confidence": {"score": 0.42, "method": "rgb_cluster_detection",
          "sources": ["Orthofoto 2024 Wien"],
          "rationale": "no regular dark cluster detected; absence of evidence is weak evidence of absence",
          "limitations": ["negative detections capped at 0.45",
                          "15 cm GSD with JPEG compression; the city's own 7.5 cm pipeline reports ~70% detection of existing installations"],
          "factors": {"shadow_fraction": 0.95}}}
    }
  }]
}
```

Validated against a committed JSON Schema; a schema failure fails the run.

---

## 9. Repository structure and execution — **ORIGINAL PROPOSAL, NOT THE BUILT REPOSITORY**

> **Historical record.** The tree and command list below are the structure *proposed* at Phase 1,
> preserved so the design decisions can be read against what was actually built. **Several entries
> were never created** — including `mosaic.py`, `render.py`, `review.py`, `DATA_SOURCES.md`,
> `Dockerfile`, `outputs/manual_review_template.csv`, and the `fetch`, `review-template`,
> `review-report` and `docker-run` make targets. Rendering, review helpers and overlay drawing
> ended up inside `pipeline.py` rather than in separate modules; Docker and the CSV review loop
> were dropped as unnecessary for a one-day scope.
>
> **For the actual structure and the actual commands, see `README.md`.** Nothing below should be
> read as a current deliverable.

```
src/propx_roofs/
  config.py  sources/{wfs.py,wmts.py}  geometry.py  mosaic.py
  units.py                       # buffered FMZK fetch, dissolve by BW_GEB_ID, null-id handling
  join.py                        # best-IoU roof-record match, containment ratios, ambiguity
                                 # detection, cardinality (1:1 / 1:n) without silent collapsing
  segment/{grabcut.py,refine.py,agreement.py}
  attributes/*.py  enrich/roof_record.py
  confidence.py  provenance.py  render.py  review.py  cli.py
  schema/roof_attributes.schema.json
configs/study_area.yaml
data/cache/                      # committed: WFS GeoJSON + z20 tiles, CC BY 4.0, small
outputs/roof_attributes.json  outputs/overlays/*.png  outputs/manual_review_template.csv
docs/{design_and_reasoning.md,phase1_design.md,study_area_selection.md}
docs/assessment/                 # git-ignored, local context only
tests/  README.md  DATA_SOURCES.md  pyproject.toml  Makefile  Dockerfile  .gitignore
```

`make recon` · `make fetch` (network only) · `make run` (deterministic, cached) · `make review-template` ·
`make review-report` · `make test` · `make lint` · `make docker-run`.

---

## 10. Risks and assumptions

**Assumptions:**
1. Orthofoto 2024 is a true orthophoto in the chosen area — documented, not yet checked against a tall
   building, which is the only test that matters.
2. ~~`ANLAGENLEISTUNG2025OGD` outlines relate to FMZK the way the 2022 outlines did.~~ **Resolved in Phase 2:
   they do not.** The join is best-overlap, not identity (§1.3, §1.4).
3. `DACHFORM` takes only `"Flachdach"` / `"Schraegdach"` — **confirmed** across 252 records in the three
   reconnaissance areas, with zero nulls. Any other value still maps to `unknown` rather than being coerced.
4. CC BY 4.0 covers committing a small tile cache. A reading of the licence, not legal advice.

**Risks:**
1. ~~z20 tiles have never been fetched.~~ **Resolved:** 1265 tiles fetched across three areas, mosaics built
   and inspected; effective resolution 0.0995 m/px as predicted.
2. `green_roof` remains the likeliest visibly-wrong output, and the reason changed: **dormant vegetation in
   the March/April imagery outweighs the missing NIR band** (§5.2). `unknown` is an accepted result.
2b. **Thin authoritative context in the selected sample** — 3 of 10 buildings have typology, 1 of 10 has a
   `BAUJAHR`. `GEBAEUDETYPOGD` and `GEBAEUDEINFOOGD` cover historic stock and most of the Sonnwendviertel is
   post-2010. Correct behaviour (`not_in_source`), thin result, stated in the README.
2c. **No `requires_visual_review` slope exemplar** in the sample: both `SLOPE_MEAN > 60°` records in the study
   bbox failed the interior/IoU filter. **The §5.1 conflict flag has no exemplar either**, on the evidence
   the implemented detector produces: vie-swv-008 was selected on a manual reading that the ridge detector
   does not support (§5.1). Both review triggers are therefore tested synthetically, and no selected
   building exercises either one. That is the honest position; forcing a real building to trigger a flag
   would be fitting the instrument to an expectation.
3. The CV stage contributes attributes, agreement evidence and an alignment diagnostic but never a primary
   polygon. That is the correct architecture; the README must frame it so it is not misread as the CV stage
   doing nothing.
4. `DACHFORM` is binary, so `complex` rests entirely on our own image and geometry evidence — the weakest
   link in the roof-type story.
5. Small buildings (§1.3 shows units of ~99–188 m²) span relatively few 15 cm pixels, so per-building
   attribute error is proportionally larger than on big flat roofs.
