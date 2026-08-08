# Design and Reasoning

> **Document status: CURRENT.** This is the maintained design summary; the phase documents under
> `docs/` are historical records and defer to this one where they disagree.

## Why these sources, and the trade-offs

I used the City of Vienna's **2024 true orthophoto** (WMTS `lb2024`, 15 cm GSD) for imagery and
the **2025 solar-cadastre roof layer** `ogdwien:ANLAGENLEISTUNG2025OGD` (WFS) for authoritative
roof geometry, cross-checked against FMZK building units (`ogdwien:FMZKGEBOGD`, dissolved by
`BW_GEB_ID`). At zoom 20 the imagery samples about 0.0995 m/px at Vienna's latitude. More
important, true-ortho processing reduces the roof displacement that would otherwise invalidate
footprint-aligned measurements.

The trade-off is season: the March/April flights have long shadows and dormant vegetation, so
green-roof evidence is weak. The 2025 roof outlines derive from a 7.5 cm laser-scan surface model;
an RGB segmenter built in one day is not a credible replacement. The authoritative polygon is
therefore canonical and segmentation is evidence about it, never a replacement. No code path can
promote a CV polygon to published geometry.

Four WFS layers are used: the 2025 roof layer, FMZK, building typology and building information.
The 2022 solar layer was queried and rejected as older; `PVPOTENZIALE2025OGD` was rejected because
it has no documented key to the roof layer. OpenStreetMap `roof:shape` was inspected but was too
uneven to verify per building. Sentinel-2 is too coarse; oblique imagery needs an unvalidated
photogrammetric solve; thermal imagery confounds material, aspect and acquisition time. DOM/DGM
rasters are the most useful next source.

## What the sources supported, and what they did not

The 2025 layer supports the outline, binary roof form (`DACHFORM`), mean slope, PV-suitability
areas, capacity and potential annual yield. EPSG:31256 supports planimetric area and boundary
measurements. The image supports cautious observations of visible surface class, ridge orientation
and RGB vegetation evidence.

I did not claim material, hipped/gable subtype, superstructures or condition because the available
sources do not validate them. Solar-panel presence is also withheld. A four-case visual check first
found the detector anti-correlated with the imagery. A later evaluation drew a 109-roof pool: a
model-assisted first pass covered all 109, I reviewed 103, six unreviewed rows were dropped, and the scoreable
reference is 90 rows — 85 strict two-reader agreements and 5 human-resolved model abstentions.
I saw the proposed labels, so the readings were anchored rather than
independent. On 35 held-out reference-negative roofs — 33
strict agreements and 2 human-resolved assistant abstentions — the detector produced 1 false
positive, Clopper–Pearson 95% [0.07%, 14.9%], failing the pre-registered bar of zero. The reference
uses the same imagery and contains only two positives, so it supports neither recall nor general
accuracy. Solar remains `null` / `unavailable` / unscored, with raw diagnostics retained.

## Alignment, and scaling from ten buildings to thousands

The orthophoto has a known tile grid, so the authoritative polygon is reprojected into pixels and
rasterised as the GrabCut seed. Consequently CV-to-authoritative IoU is agreement, not accuracy.
The authoritative-to-authoritative join is less direct: the 2025 outline and FMZK dissolve use
different cadastral cuts. A 150 m buffered fetch, best-IoU match and recorded second-best overlap
keep ambiguity and 1:n cardinality visible.

At portfolio scale I would bulk-load vector layers into PostGIS or GeoParquet and perform one
spatial join rather than per-building WFS calls. Imagery work should be partitioned by tile id so
neighbouring roofs share downloads and reads; buildings remain independently processable inside
each partition. Manual inspection becomes exception triage driven by ambiguity, review and
low-confidence flags. A labelled monitoring set would be required before treating any image
detector or confidence score as calibrated.

## How confidence is represented and would be used

Confidence is `base(provenance) × Π(penalties)`, capped per attribute. Authoritative provenance has
the highest ceiling; image-derived attributes have lower caps. Penalties expose specific evidence
such as source recency, image conflict, shadow or weak typology overlap. Every score travels with
availability, method, sources, rationale and limitations; abstentions have no score.

These values are documented heuristics, not probabilities. A score of 0.9 means authoritative
source with little recorded penalty, not “correct nine times in ten”. They are suitable for
ordering a review queue or applying a conservative gate, but not for expected-value arithmetic or
accuracy reporting. The schema allows calibrated probabilities to replace them later without
changing the consumer-facing record shape.
