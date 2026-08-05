# Design and Reasoning

## Why these sources, and the trade-offs

I chose the City of Vienna's **2024 true orthophoto** (WMTS `lb2024`, 15 cm GSD) for imagery and the **2025
solar-cadastre roof layer** `ogdwien:ANLAGENLEISTUNG2025OGD` (WFS) as the authoritative roof geometry,
cross-checked against **FMZK building units** (`ogdwien:FMZKGEBOGD`, dissolved by `BW_GEB_ID`).

Resolution decided the imagery. At zoom 20 the effective ground sample is 0.0995 m/px at Vienna's latitude,
so a 1 m dormer is ten pixels across — enough for texture, ridges and panel grids. The **true**-ortho
property matters more: in a conventional orthophoto a roof is displaced from its footprint by roughly
building height × off-nadir angle, silently invalidating every footprint-aligned measurement. Coverage is
city-wide, licence CC BY 4.0, cost zero.

The main trade-off is **season**: the flights (19/20 and 29 March, 12 April 2024) predate leaf-out, so
shadows are long and vegetation dormant, and green-roof evidence is published as a low-confidence RGB
observation rather than a finding.

I did **not** use single-source CV for geometry. The 2025 outlines derive from a 7.5 cm laser-scan surface
model; no RGB segmentation I can build in a day will beat that. So the authoritative polygon is canonical
and segmentation is *evidence about* it, never a replacement — enforced structurally: no code path promotes
a CV polygon to the published outline.

**Sources actually queried** (live responses in `docs/phase1_design.md` §1.1): WMTS capabilities
for `lb2024`, and six WFS layers, of which **four are used** — the 2025 solar layer, FMZK, building
typology, building info. Two were queried and dropped: the 2022 solar layer (older geometry, no yield
range) and `PVPOTENZIALE2025OGD` (no documented key to the 2025 layer). Neither is in the cache,
`run.sources` or the output.

**Inspected, then rejected:** *OpenStreetMap* `roof:shape` — unevenly populated, unverifiable per building.

**Sources considered but not queried at all**, with reasons: *Sentinel-2* — 10 m bands cannot resolve a
20 m roof into planes; *oblique, street-level and Mapillary imagery* — not orthorectified, so measurement
needs a photogrammetric solve I could not validate in time; *Cesium ion and 3D meshes* — derived products
whose provenance I cannot audit, where Vienna publishes the underlying surface model directly; *thermal
layers* — insulation inference confounds with material, aspect and time of day, with no ground truth. None
of the four were attempted; I do not report experiments I did not run. The DOM/DGM rasters are the obvious
next source.

## What the sources supported, and what they did not

Authoritative, from the 2025 layer: **outline, roof type (`DACHFORM`), mean slope, PV suitability sub-areas
and capacity**. Derived in EPSG:31256: **area**, perimeter, boundary diagnostics.

Observed from imagery: **roof surface class, ridge orientation, green-roof RGB evidence** — each carried
with segmentation agreement, shadow fraction and a seed-independent boundary-gradient ratio.

Not extracted, deliberately: **material** — separable by eye, but with no labelled Vienna sample to
validate a classifier I publish a coarse surface class instead; **hipped/complex subtypes** — the
authoritative field is binary and a *mean* slope cannot distinguish a hip from a gable; **superstructures
and condition** — no validation set.

**Solar is the one attribute I withheld.** The detector runs, but a spot check **against the imagery** found
it anti-correlated on all four checkable cases. That check is a reviewer's visual read, not ground truth,
and no independent reference exists — `ANLAGENLEISTUNG` is modelled PV *potential*, not installed capacity.
Publishing a boolean from a detector that inverts the only available check is worse than publishing
nothing, so the value is `null`, availability `unavailable`, the score `null` rather than low. Raw
diagnostics stay in the file for a future validation.

## Alignment, and scaling from ten buildings to thousands

Geolocation is not an image-registration problem here: the imagery is a true ortho in a known tile grid, so
the authoritative polygon is reprojected into the pixel frame, rasterised, and used as the segmentation
seed. A roof is never matched to the wrong building by appearance. The real matching problem is
authoritative-to-authoritative — the 2025 outline and the FMZK dissolve are one building but not the
same polygon (both containment ratios sit symmetrically near 0.98), solved by best-IoU overlap on a 150 m
buffered fetch, with the second-best candidate recorded so ambiguity stays visible.

Scaling changes the problem in three ways.

**Fetching.** Per-building WFS calls do not scale. Bulk-download both vector layers once into a
spatially-indexed store (PostGIS or GeoParquet) and the join becomes one spatial query, not ten thousand
requests. Tiles fetch by tile id, so neighbours share them.

**Compute.** Buildings are independent, so this is embarrassingly parallel — but partition by *tile id*, not
building id, so workers share tile reads. The pipeline is already a pure function of (cache, config), so
that is safe.

**Review.** Inspecting ten buildings by hand becomes triage, not inspection. The ambiguity, review and
low-confidence flags emitted here are the queue, and publishing abstentions rather than guesses makes them
sortable.

The honest limitation: nothing here is calibrated. At thousands I would need a labelled sample, and
building one is the first work I would do.

## How confidence is represented and would be used

A score is `base(provenance) × Π(penalties)`. Provenance sets the ceiling —
authoritative 0.95, image-derived 0.80 under a lower per-attribute cap (0.75 solar, 0.70 ridge, 0.65 green
roof, 0.60 surface class; 0.85 is the *derived*/geometry cap) — and penalties reduce it for shadow, low
agreement, boundary ambiguity or conflict. The highest observed score published is 0.70. Each score ships
with its availability, method and evidence.

These are **documented heuristics, not calibrated probabilities**. A 0.9 does not mean nine times in ten; it
means "authoritative source, no penalty fired". That makes them safe for *ordering* and *gating* and unsafe
for arithmetic: use them to route records to review, filter a portfolio query above a threshold, or decide
which of two disagreeing sources to surface — never to multiply into an expected value or report as
accuracy. Anything stronger needs the labelled sample above; the schema is shaped so that replacing
heuristic scores with calibrated ones changes no consumer's code.
