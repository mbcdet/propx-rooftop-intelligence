# Vienna Rooftop Intelligence

This project explores a practical question: how much trustworthy roof information can be produced
for a small building portfolio using only open Vienna data?

I selected ten buildings around Sonnwendviertel and Wiedner Gürtel and combined the City of
Vienna's 2024 orthophoto with its 2025 roof and building layers. The result is one structured
record per building, including roof geometry, useful attributes, provenance, confidence and
review notes.

The main design decision is simple: the city's roof polygon remains authoritative. The image
pipeline produces a second polygon as supporting evidence, but it never replaces the official
geometry. Because GrabCut is seeded from the city outline, agreement between the two polygons is
useful for quality control, not a measure of segmentation accuracy.

For the reasoning behind this choice, see
[`docs/design_and_reasoning.md`](docs/design_and_reasoning.md).

## Run it

Python 3.11 is the locked environment used for the submitted outputs. Python 3.12 is also
supported for normal runs. The required imagery and vector data for the ten buildings are already
cached in the repository, so the pipeline runs offline.

```bash
make install
make run
```

This creates:

- [`outputs/roof_attributes.json`](outputs/roof_attributes.json), containing ten
  schema-validated building records;
- [`outputs/overlays/`](outputs/overlays/), containing one visual overlay per building.

For the exact submitted environment on CPython 3.11.3 / macOS arm64:

```bash
make install-locked
```

Useful verification commands:

```bash
make cache-verify
make verify-repro-semantic
make verify-repro
make test
make lint
```

`verify-repro-semantic` compares every published value, coordinate, availability state and review
flag while ignoring runtime metadata. It is the portable check across supported environments.
`verify-repro` is stricter: under [`requirements.lock`](requirements.lock), it compares the JSON
and overlays byte-for-byte, apart from the Git state recorded in run metadata.

CI runs the semantic check on Linux. It permits up to 2.0 units of cross-platform drift only in
raw image diagnostics (the observed maximum is a 1.92-degree Hough-angle difference); published
values, geometry, availability and review flags still use the strict base tolerance.

Installing the package also provides the `propx-roofs` command. When it is used outside the
checkout, set `PROPX_ROOFS_DATA_ROOT` or pass `--cache-root` so it can find the cached data. Normal
pipeline runs are offline; only `propx-roofs cache-build` and `make recon` contact Vienna's data
services.

## Start with these results

The complete result is in the JSON file, but these five overlays show the most useful range of
behaviour. Amber is the official roof outline, magenta is the image-derived candidate, and green
marks accepted ridge evidence where one survives the quality gates.

| Building | Why it is useful |
|---|---|
| [vie-swv-001](outputs/overlays/vie-swv-001.png) | A clear flat courtyard roof. The inner void is excluded from area. Solar panels are visible, but the detector result is deliberately withheld after failing evaluation. |
| [vie-swv-007](outputs/overlays/vie-swv-007.png) | The only accepted ridge estimate, 65.04 degrees. The supporting line is shown directly, while topology disagreement with the CV polygon sends the record to review. |
| [vie-swv-008](outputs/overlays/vie-swv-008.png) | The city source and the image appear to disagree about roof form. The authoritative value is retained and the suspected conflict is made explicit. |
| [vie-swv-010](outputs/overlays/vie-swv-010.png) | Only 68.2% of the outline is judgeable. Ridge and green-roof observations therefore abstain instead of publishing weak negatives. |
| [vie-swv-004](outputs/overlays/vie-swv-004.png) | A curved, 243-vertex roof outline that tests the geometry and segmentation path. |

These captions are visual QA observations, not survey ground truth.

## How it works

The run has eight main steps:

1. Verify the cached imagery and vector requests against the current study-area configuration.
2. Load the ten pinned roof records and dissolve building parts by `BW_GEB_ID`.
3. Match each roof to the best-overlapping building unit without silently collapsing ambiguous or
   one-to-many cases.
4. Reproject the official polygon into the image and use it to seed OpenCV GrabCut.
5. Extract source-backed attributes and cautious image evidence from the roof mask.
6. Calculate all published areas and distances in **EPSG:31256**.
7. Attach provenance, availability, confidence rationale and limitations to every attribute.
8. Validate the complete result against JSON Schema before publishing it atomically.

The data sources have distinct roles:

| Role | City of Vienna source |
|---|---|
| Imagery | 2024 true orthophoto, WMTS `lb2024`, 15 cm native resolution |
| Roof outline, form and mean slope | `ANLAGENLEISTUNG2025OGD` |
| Building identity and geometry cross-check | `FMZKGEBOGD` |
| Construction context | `GEBAEUDETYPOGD` and `GEBAEUDEINFOOGD` |

The implementation uses NumPy, OpenCV, Shapely, PyProj, Pillow and JSON Schema. There are no
trained weights. Image-derived attributes use deliberately narrow, inspectable rules: geometric
refinement for the candidate polygon, gated gradient evidence for ridge orientation, and cautious
RGB evidence for visible surface and vegetation. A value is withheld when the image cannot support
it.

## Output contract and confidence

The output keeps official geometry, image evidence and attributes separate. A shortened record
looks like this:

```jsonc
{
  "building_id": "vie-swv-001",
  "authoritative_roof_polygon": { "type": "MultiPolygon", "crs": "EPSG:4326" },
  "cv_candidate_polygon": { "type": "Polygon", "crs": "EPSG:4326" },
  "delineation": {
    "primary_polygon": "authoritative_roof_polygon",
    "agreement_with_authoritative_geometry": { "iou": 0.9685 }
  },
  "attributes": {
    "roof_area_m2": {
      "value": 2319.5,
      "availability": "derived",
      "confidence": {
        "score": 0.85,
        "method": "projected_area_epsg31256",
        "rationale": "planimetric area of the authoritative outline",
        "limitations": ["horizontal projection, not sloped surface area"]
      }
    }
  },
  "review_flags": []
}
```

Availability is always explicit: `authoritative`, `derived`, `observed`, `inferred`,
`unavailable` or `not_in_source`. Confidence scores are documented heuristics for review ordering,
not calibrated probabilities. A missing value is never silently turned into `false` or zero.

## Data preparation and checks

Several unglamorous details were important to making the result defensible:

- FMZK is fetched with a 150 m buffer so buildings at the query edge are not clipped. Parts without
  `BW_GEB_ID` are excluded and counted.
- Source fields change type between layer versions, and the 2025 `YR` field is empty. Parsing is
  defensive, and annual potential uses the replacement min/max fields.
- Unknown roof-form values stay unknown. Image observations may lower confidence or raise a review
  flag, but they cannot overwrite an authoritative value.
- Every cached file has a SHA-256 hash. Cache verification also checks request parameters, feature
  counts, bounds, geometry validity, coordinate ranges and required identifiers.
- Each output records its schema, Git state, runtime, dependencies and cache-manifest hash.

## Why solar panels are withheld

The solar detector runs and its raw diagnostics remain in the result, but the published value is
`null` with `availability: "unavailable"`.

This was a measured decision. In a pre-registered held-out evaluation, the detector produced one
false positive among 35 confirmed negatives, failing the zero-false-positive acceptance rule. The
reference set contained only two positive roofs, so it cannot support a useful recall or general
accuracy claim either. Publishing an uncertain Boolean would have been less honest than
abstaining. The method, labels and Clopper-Pearson interval are documented in
[`docs/solar_evaluation.md`](docs/solar_evaluation.md).

## Limitations

- The ten buildings were selected for source availability and visual diversity; they are not a
  representative sample of Vienna.
- There is no independent roof ground truth or calibrated confidence model.
- March and April RGB imagery makes dormant green roofs difficult to distinguish.
- Roof area is planimetric, not actual sloped surface area.
- Material, condition, roof subtype and superstructures are not claimed because the available data
  cannot validate them reliably.
- The final artifacts and all ten overlays were reviewed and bound to their hashes on 8 August
  2026. This is a human quality-control step, not an accuracy study.

## From prototype to service

This is an offline batch prototype, not a deployed API. For a portfolio-scale service I would keep
the same versioned record contract but separate ingestion from assessment. An asynchronous
`POST /v1/roof-assessments` endpoint would create jobs; source snapshots, configuration and model
versions would be immutable run inputs; vectors would live in PostGIS or GeoParquet; and imagery
would be tile-partitioned so neighbouring buildings share reads. Ambiguous or low-confidence cases
would enter a review queue. A labelled monitoring set would gate releases and make rollback
possible without changing the consumer-facing schema.

With more time, I would first build an independently labelled sample. I would then add Vienna
DOM/DGM rasters for true roof planes, slope and aspect; validate or retire the solar detector; add
summer imagery for vegetation evidence; and replace per-building data access with bulk spatial
joins.

## Repository guide

| Path | Contents |
|---|---|
| [`outputs/roof_attributes.json`](outputs/roof_attributes.json) | Main structured result |
| [`outputs/overlays/`](outputs/overlays/) | Ten review images |
| [`docs/design_and_reasoning.md`](docs/design_and_reasoning.md) | One-page design and trade-offs |
| [`docs/solar_evaluation.md`](docs/solar_evaluation.md) | Solar-detector evaluation |
| [`src/propx_roofs/`](src/propx_roofs/) | Pipeline, image analysis, confidence, CLI and schema |
| [`tests/`](tests/) | 411 offline tests |

## AI assistance

I defined the problem framing, architecture, data-source strategy, acceptance criteria and final
trade-offs. I also ran and inspected the pipeline and its submitted outputs, evaluated review
findings and made the final decisions on every correction. Claude and OpenAI Codex were used as
supporting tools for targeted implementation assistance, code review, test development and
editorial feedback. I verified and take full responsibility for the submitted result.

## Licence and attribution

All input data © City of Vienna, **CC BY 4.0**. The required attribution is included in every
output record:

> Datenquelle: Stadt Wien - data.wien.gv.at
