# Phase 3 — Visual and Manual Validation

**Status: model-assisted visual QA complete; this is not ground truth or human validation.**
Baseline commit `c86cbf4`. Nothing was changed by *Phase 3*; `run.manual_review` remains
`not_yet_reviewed` because no human validation is claimed.

**Amended by Phase 4A.** Phase 4A acted on findings 1, 4 and 7 below and corrected several factual
errors in this report — the vie-swv-007 ring table (mislabelled as a decomposition), 007's withheld
solar verdict (`false`, not `true`), the claim that GrabCut produced no holes for any building, and the
overreach that the shadow abstention path "cannot execute". The corrections are marked inline. The
reviewers' observations themselves are unchanged, and `validation/visual_audit.json` still carries the
sha256 of the document they actually inspected rather than the regenerated one.

---

## 1. Method, and what it is not

Three reviewers worked read-only and offline against the committed artefacts:

| Reviewer | Scope |
|---|---|
| **A** | visual inspection, vie-swv-001 … 005 |
| **B** | visual inspection, vie-swv-006 … 010 |
| **C** | methodology, honesty, schema/output consistency, and what RGB can legitimately support |

**These observations are not ground truth.** They are model-assisted readings of the *same* single-epoch
15 cm RGB true-orthophoto the detectors read (flights 19/20 March, 29 March, 12 April 2024), at ~10 cm/px.
A reviewer agreeing with a detector is therefore **not an independent measurement** — it is two readings of
one image. Nothing here is an accuracy, precision, recall or performance figure, and no published value was
or may be changed on the basis of this audit alone.

### What a nadir RGB orthophoto can and cannot settle

| Field | Verdict |
|---|---|
| Outline alignment | **CAN** — the strongest case. But nadir shows the eave/parapet, not the wall line, and a 2025 roof record against 2024 imagery makes a real epoch mismatch and a segmentation error look identical |
| CV candidate | **CAN**, as plausibility only. There is no truth boundary in the image |
| Roof type | **PARTIAL — inference, never measurement.** Monoscopic nadir RGB contains *no direct slope information*. Cannot distinguish 10° from 20°, and **cannot adjudicate `SLOPE_MEAN = 42.92°` on vie-swv-008 at all** |
| Ridge azimuth | **CAN** (planimetric). Ridge *presence* is **PARTIAL** — parapets, seams, plant rows and array edges all present as lines |
| Surface class | **PARTIAL — appearance only.** Clay tile vs tile-look metal, gravel vs grit-finished bitumen, dry sedum vs gravel are not separable at 15 cm |
| Solar | **PARTIAL — presence yes, absence never, type never.** PV vs solar-thermal is undeterminable in RGB |
| Green roof | **CANNOT, in the general case.** No NIR, dormant season. The human reviewer faces the identical physical limit |
| Image quality | **CAN — fully.** The one field where a human is a legitimate independent check |
| Review routing | **CAN** — it is a judgement, not a measurement |

**Physically outside this imagery, full stop:** slope angle, height, per-plane aspect, construction material,
roof build-up, vegetation vigour, change over time, PV vs solar-thermal.

---

## 2. Ten-building audit table

Assessments are the reviewers'; published values are quoted from `outputs/roof_attributes.json`.

| id | outline | CV candidate | roof type (vis / auth) | ridge | surface | solar (vis / withheld) | green | image quality |
|---|---|---|---|---|---|---|---|---|
| 001 | aligned | aligned | flat / flat → **supports** | – | plausible | **visible** / `false` | unclear | good |
| 002 | **questionable** | aligned | flat / flat → **supports** | – | questionable | not_visible / `false` | **candidate** | good |
| 003 | aligned | aligned | unclear / pitched → **abstain** | – | plausible | **visible** / `false` | none | good |
| 004 | **questionable** | aligned | unclear / pitched → **abstain** | – | plausible | not_visible / `false` | unclear | good |
| 005 | aligned | **questionable** | flat / flat → **supports** | – | plausible | not_visible / `false` | none | good |
| 006 | aligned | aligned | flat / flat → **supports** | – | questionable | not_visible / **`true`** | **candidate** | good |
| 007 | aligned | **questionable** | pitched / pitched → **supports** | 65.0° **plausible** | questionable | not_visible / `false` | none | **questionable** |
| 008 | **questionable** | questionable | flat / pitched → **conflicts (visual)** | 65.2° **questionable** | questionable | not_visible / `false` | none | **questionable** |
| 009 | **questionable** | questionable | unclear / pitched → **abstain** | – | plausible | not_visible / `false` | none | **questionable** |
| 010 | **questionable** | aligned | unclear / pitched → **abstain** | 64.7° **questionable** | questionable | not_visible / **`true`** | none | **questionable** |

### Descriptive counts — judgeable cases only

> Counts over 10 purposively selected buildings. **Not a performance estimate, not generalisable.**
> **n=10 manual spot check, not a validation metric.**

- Authoritative outline: 5 of 10 aligned, 5 questionable, 0 not judgeable.
- CV candidate: 6 of 10 aligned, 4 questionable, **0 failed** (no segmentation failure in the sample).
- Roof type, where the reviewer could form a view (6 of 10): 5 support the authoritative value, 1 visually
  conflicts (008). The other 4 are `unclear` → abstain.
- Ridge: published on 3 of 10. 1 judged plausible (007), 2 questionable (008, 010).
- Image quality: 6 of 10 good, 4 questionable — all four for shadow or unreadable regions that the record
  reports as `shadow_fraction: 0.0`.

---

## 3. Solar candidate spot check

> **This is a tiny visual spot check, not a performance estimate.** The published `solar_panels.value` is
> `null` on all ten buildings, so there is nothing to score. The table below compares reviewer reads against
> the **withheld** `image_evidence.quality.withheld_detector_verdict`, which the record itself
> labels "NOT a Boolean observation". **n=4 checkable cases. No rate, no percentage, no accuracy claim.**

| | reviewer reads **visible** | reviewer reads **not visible** |
|---|---|---|
| withheld verdict `true` | 0 | **2** (006, 010) |
| withheld verdict `false` | **2** (001, 003) | 6 |

**The detector is anti-correlated on every checkable case**, and both reviewers traced the mechanism:

- **001** — the detector *found* the array (932.8 m² cluster, texture 34.1, periodicity 0.598) and rejected it
  as `not_rectangular_enough` (0.455) because the array is **ring-shaped**.
- **003** — a 287.8 m² cluster covering 26.2% of the roof was **accepted**; the negative came from the regularity gate instead (one cluster only, periodicity 0.1331 against a 0.15 threshold — a 0.017 miss on a generic parameter).
- **006** — the accepted 359.6 m² "cluster" covering 59.6% of the roof **is the mottled substrate field**.
- **010** — the accepted cluster is 14.3 m² on the deck; the unmistakable module field is on the
  **neighbouring building**, several metres east across a clear roof gap, entirely outside the polygon.

This substantiates the withholding decision. **All published solar values remain `null` and must stay `null`.**

**Circularity warning (Reviewer C).** These buildings were chosen partly *because of* how solar looks on them,
and `solar_panels.max_value` (145) and `solar_internal_texture_min` (25.0) were set from pixel measurements on
vie-swv-001 and vie-swv-003. **The held-out sample size for solar is 0.**

> **Superseded as evidence, not as observation.** Reviewer C's warning was acted on: a separate
> evaluation on 109 roofs across three other Vienna zones, with a pre-registered threshold and a
> genuinely held-out split, is in [`docs/solar_evaluation.md`](solar_evaluation.md). It confirms
> the abstention with a number — **1 false positive on 35 reference-negative roofs: 33 strict
> two-reader agreements and 2 human-resolved assistant abstentions, 95% CI [0.07%, 14.9%]**,
> against a bar of zero. The four cases below remain what they always were: four
> visual reads, too few to conclude from. The reviewers' observations are unchanged.

---

## 4. vie-swv-007 — the boundary alignment warning

**The warning was measuring the wrong thing.** `hausdorff_m` was computed on the full polygon
`.boundary`, which includes interior rings. The figures below are **independent directed maxima**,
not a decomposition — a Hausdorff distance is a maximum, so they do not sum to 5.705 m; the
largest of them *is* 5.705 m:

| component | **directed** max distance to the nearest point of the CV boundary |
|---|---|
| authoritative exterior ring | 1.472 m |
| authoritative **interior ring 0** (1.21 m²) | **5.704 m** ← sets the published figure |
| authoritative interior ring 1 (0.69 m²) | 4.525 m |
| CV boundary → authoritative boundary (reverse) | 1.049 m |
| full symmetric boundary Hausdorff | **5.705 m** |
| exterior-rings-only symmetric Hausdorff | **1.476 m** |

Note: `shapely.hausdorff_distance` is *symmetric*. Symmetric ring-vs-entire-CV-boundary gives
21.572 m and 30.092 m — geometrically meaningless here and **not** contributions to the 5.705 m
maximum.

The authoritative geometry carries **two ~1 m² light shafts**; the CV candidate is a plain polygon with **no
holes** *on this building*: both shafts are ~1 m², below the segmenter's 4.0 m² minimum hole area.
(GrabCut does produce holes elsewhere — one on vie-swv-001, two on vie-swv-002.) The 5.704 m is
the distance from a light-shaft vertex to the
nearest perimeter — a **topology mismatch**, not disagreement about where the roof edge is. Along the actual
perimeter the two estimates agree within 1.5 m, better than vie-swv-009, which fired no warning.

The flag is technically correct against its threshold and its note is properly hedged. But it is measuring
hole-vs-no-hole, and design §4.2 anticipated exactly this question.

**Resolved in Phase 4A.** `hausdorff_m` is now exterior-rings-only and the warning is driven by
`exterior_iou` plus that figure, so vie-swv-007 no longer fires: 1.476 m against a 5.0 m
threshold. The ring-count difference is reported separately as `topology_mismatch`, a diagnostic
that routes nowhere. **The sample now has zero fired warnings and zero review flags**, and the
all-rings figure is retained as `hausdorff_full_boundary_m`.

---

## 5. vie-swv-008 — the manual hypothesis, unresolved and now better evidenced

Reviewer B read this independently and **agrees with the §5 manual hypothesis**: the visible roof surface reads
predominantly flat — a bright near-uniform deck, skylight strips, a raised penthouse, mechanical plant, and no
pair of differently-facing pitched planes.

**New evidence: a probable facade inside the roof outline.** The ~38 m south-east side encloses a 5–8 m wide
band showing **regularly spaced window openings with glazing bars, frames and reveals**, seen obliquely. Either
residual building lean — meaning the orthophoto is **not fully true-ortho at this tall building**, which design
§10 assumption 1 records as untested — or a stepped setback whose wall is visible in plan. Either way a
near-vertical surface lies inside a published `roof_area_m2`, which is **consistent with** §5 hypotheses 1 and 3
for a 42.9° whole-roof mean on a flat-looking roof.

**The detected 65.24° ridge is questionable.** It sits **0.24° from the footprint long axis (65.00°)**, and the
strongest ~65° edge in the outline is the deck/facade boundary (deck ≈201 vs band ≈151–116) — easily enough for
the reported 23.1% contrast, with interior erosion of only 1.5 m against a 5–8 m wide band.

**Status: unresolved, exactly as `study_area_selection.md` §5 says.** The authoritative value publishes
unmodified. Two eyeball readings of one orthophoto are not two instruments, and the 2023 laser-scan DOM behind
`SLOPE_MEAN` is a better instrument than either. Nothing was retuned to make the flag fire.

---

## 6. Green roof

Both designated candidates return `false` at the 0.45 negative cap. **This is the dormant-season failure mode
design §5.2 predicted, and it is not evidence that those roofs are not vegetated.**

- **002** — large reddish-brown wing surfaces consistent with dormant extensive sedum; greenness genuinely
  absent (ExG > 0.06 on < 0.5%).
- **006** — reviewer B calls it a **visible candidate on texture and layout, not colour**: planted-looking bays
  bounded by a paver walkway grid and a gravel margin is a characteristic extensive-green-roof layout. Measured
  ExG is **−0.021 to −0.031**, i.e. *less green than neutral*.

Neither reading may be promoted to ground truth. Reddish ballast and dormant sedum are not separable at
10 cm/px without NIR or a second season.

---

## 7. Findings and disposition

**Raised by the reviewers. Each carries its own disposition — CLOSED where a later phase acted
on it, UNRESOLVED where nothing has changed.** The original wording is preserved in every case,
so the audit trail shows what was found as well as what was done about it.

1. **CLOSED IN PHASE 4A — a published sentence was contradicted by the repo's own comments**
   (Reviewer C, most serious).

   *As found:* `outputs/roof_attributes.json` stated on all ten buildings, and the schema repeated:
   *"thresholds were deliberately NOT adjusted to fit the ten selected buildings."* But
   `configs/pipeline.yaml` documents in detail that `max_value` was moved 110 → 145 from measurements
   on **vie-swv-001 and vie-swv-003**, and `attributes/__init__.py` says `solar_internal_texture_min`
   was *"measured over the ten selected buildings"*. The published sentence overstated it during drafting.

   *Phase 4A correction at the time* — the output rationale, schema description and docstring then
   stated consistently:

   - **two brightness/texture gates** (`image.solar_panels.max_value` and
     `solar_internal_texture_min`) **were calibrated from measured pixels on vie-swv-001 and
     vie-swv-003** inside the selected sample;
   - **there was no held-out solar case at that phase** — the buildings anchoring calibration were
     the same ones used by the check;
   - **the detector verdict remains withheld**: `value` is `null`, `availability` is `unavailable`,
     `confidence.score` is `null`, and the raw candidate diagnostics are retained.

   No threshold was moved to close this finding; only the claim about the thresholds changed. A
   later non-ground-truth reference evaluation is now complete (`docs/solar_evaluation.md`), failed
   its pre-registered false-positive bar, and supersedes the earlier "pending validation" wording;
   it still supports neither recall nor general accuracy.
2. **The image evidence is not independent of the authoritative label.** `ridge_plane_contrast_min: 0.15` was
   placed in the measured gap between *this sample's* `Flachdach` and `Schraegdach` classes. Any statement of
   the form "the image evidence agrees with `DACHFORM`" is circular and must not be written.

   *Disposition:* **UNRESOLVED, and permanently so on this sample.** The threshold was not moved — moving it
   would fit the instrument to the expectation. The constraint is honoured as a **writing rule**: no
   agreement-with-`DACHFORM` claim appears in the output, the README or the design docs, and the ridge
   observation is published as evidence beside the authoritative value rather than as a check on it.
   Removing the circularity needs a held-out labelled sample, which does not exist.
3. **The conflict trigger is one-directional.** `image_roof_type()` returns `"pitched"` or `None` — never
   `"flat"`. So `authoritative_image_conflict` can only fire on `Flachdach` + a detected ridge, the *opposite*
   of the case that motivated it. **No detector reading of vie-swv-008 could have fired it.**

   *Disposition:* **UNRESOLVED, accepted and documented.** The detector was not changed: teaching it to
   return `"flat"` means asserting absence from a null ridge result, which this sample cannot support (see
   finding 4 — the shadow abstention path is never reached, so a null has no confidence attached to it).
   The consequence is recorded as Risk 2c in `docs/phase1_design.md` §10 — the conflict flag has no
   exemplar in the sample and is exercised only by synthetic tests. vie-swv-008's disagreement is still
   visible in the record, carried as image evidence beside the authoritative `Schraegdach`.
4. **The shadow abstention path is not exercised by these ten records.** It is *not* dead code — `tests/test_attributes.py` exercises both abstention branches on synthetic crops. Measured inside the roof masks from the committed tiles:
   the darkest pixel across all ten roofs is **62**, against a threshold of **55** — `shadow_fraction: 0.0` is a
   true measurement, but on this sample `shadow_fraction_abstain` (0.35), the `shadow_heavy` penalty
   and the `unknown` route for `green_roof` are never reached. Four reviewer-visible shadowed roofs
   publish `judgeable_fraction: 1.0`. **Phase 4A** publishes `min_luminance`, `p01_luminance` and
   `p05_luminance` beside `shadow_fraction` so the 0.0 is self-evidencing, and removed the
   "adequate image quality" wording that rested on a gate this sample cannot fail.
5. **Confidence originally carried almost no per-building information.** At this checkpoint all ten
   buildings shared one score per attribute, and only `source_recency` fired. **Do not compute a mean or
   read any score as a probability.**

   *Disposition:* **PARTLY CLOSED by the final audit.** Typology enrichment now applies the measured
   best-overlap fraction as a transparent heuristic factor, clipped at the configured 0.50 floor. The three
   available typology scores therefore changed from 0.902 to 0.600 (`vie-swv-005`) and 0.451
   (`vie-swv-007`, `vie-swv-010`). Other attributes still share scores where their recorded evidence is the
   same; no variation was invented. The README and `docs/design_and_reasoning.md` continue to state that
   these are heuristic reliability indicators suitable for review ordering, not calibrated probabilities.
6. **Stale design text.** §8's worked examples (0.63 / 0.86 / 0.42) do not reproduce; §5's rows for
   `solar_panels` and `roof_subtype` describe superseded behaviour; §5.1's slope fallback is **read by no code**;
   §7's manual-review workflow does not exist; §9's repo structure lists files that were never created. There
   is **no README**, so every caveat the design routes "to the README" currently reaches no reader.

   *Disposition:* **PARTLY CLOSED.** ~~There is no README~~ — **CLOSED:** `README.md` now exists at the
   repository root and carries the caveats the design routed to it. `docs/phase1_design.md` §7 and §9 are
   now both marked **ORIGINAL PROPOSAL, NOT THE BUILT REPOSITORY**, and §5.1's slope fallback keys are
   marked "specified, not implemented" in `configs/pipeline.yaml`. **UNRESOLVED:** §8's worked examples and
   §5's superseded rows are still stale; they are historical design text and were left rather than
   back-edited, since rewriting a Phase 1 record to match the built system would destroy the audit trail
   this document exists to preserve. The original observation above is preserved as written.
7. ~~**Segmentation thresholds live outside the hashed config.**~~ **Closed in Phase 4A:** those
   constants are now fingerprinted by `algorithm_parameters_hash`, published beside `config_hash`.
   It is a parameter-value fingerprint, not a source-code hash.
8. **PARTLY SUPERSEDED IN PHASE 4A — open per-building questions:** 004's inner area (podium deck or uncut
   courtyard — drives 3364.35 m²); 006's 3–4 dark voids (~10–12% of area, no interior rings); 005's bright
   southern strip (canopy or roof); 002/005 sharing
   `boundary_gradient_ratio` 1.4043 to 4 dp.

   *Disposition:* the first three remain **UNRESOLVED** — they need the DOM/DGM rasters or a second epoch,
   neither of which is in scope. The fourth is **SUPERSEDED:** after the Phase 4A regeneration the two
   values are 002 = 1.3851 and 005 = 1.3984, which are not equal, so the coincidence that prompted the
   question no longer exists. The original observation is kept because it was true of the document the
   reviewers read (`baseline_commit` c86cbf4).

### Reviewer disagreement, preserved

- **007's ridge.** A pre-existing lead suspicion held that ~65° might be following eaves or the street bearing.
  **Reviewer B examined it and could not falsify it** — a genuine two-plane ridge with a dark self-shadowed
  plane above and a lit tiled plane below. The perpendicular-to-long-axis concern **does not apply here**
  (elongation 1.154, near-square, so the axis is arbitrary). The concern **does** stand on 010 (elongation
  1.672, well-defined axis, ridge ~74° off it, only 3 supporting segments).
- **008's roof type.** Reviewers read the surface as flat; the authoritative source and the ridge detector say
  pitched. Preserved as a disagreement, not resolved.

---

## 8. Human-review checklist before operational use

Work through these against `outputs/validation/contact_sheet.png` and the individual overlays.

- [ ] **008 — is the south-east band a facade?** The single most consequential call. Bears on whether the
      orthophoto is truly true-ortho at a tall building, and on `roof_area_m2`.
- [ ] **008 — is the ~65.2° feature a ridge, or the deck/facade edge?**
- [ ] **010 — is the ~64.7° feature a ridge, or the lit-deck/dark-courtyard brightness step?**
- [ ] **010 — confirm the PV modules lie entirely on the neighbouring building.**
- [ ] **001 and 003 — confirm these are module arrays** and that withholding is the intended outcome given the
      rectangularity gate rejects ring and split arrays.
- [ ] **004 — podium deck or uncut courtyard?** Drives `roof_area_m2` 3364.35 m².
- [ ] **006 — dark voids: open light wells or glazed rooflights?** And: green roof or reddish ballast?
      (Candidate only — do not promote.)
- [ ] **005 — is the bright southern strip roof or canopy?**
- [ ] **007 — confirm that the topology mismatch diagnostic is understandable** and does not imply that
      either polygon is superior. The exterior-only alignment warning no longer fires.
- [ ] **Confirm the shadow-threshold behaviour as a documented dataset limitation** (finding 4): the
      darkest roof pixel in this sample is 62 against a threshold of 55, so the abstention route is not
      exercised here. The measured `min`/`p01`/`p05` luminance is now published beside
      `shadow_fraction`. This is a confirmation that the limitation is recorded honestly, **not** a
      request to retune the threshold against these ten buildings.

If completed, sign-off would be recorded by a human rather than inferred from this document.
`run.manual_review` intentionally remains `not_yet_reviewed` in the submitted output.
