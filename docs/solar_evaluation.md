# Evaluating the rooftop solar detector — a measured abstention

> **Document status: CURRENT.** The evaluation and its outcome (the withheld `solar_panels`
> verdict) still govern the published output.

**Outcome: the detector is disqualified against a pre-registered threshold. The published
`solar_panels` value stays `null` / `unavailable` / `null`, and `ATTRIBUTE_PARAMS` is unchanged.**

This document is the whole record: why the evaluation was run, how the reference labels were
made and how much they can be trusted, what was fixed in advance, what was measured, and what
the sample cannot answer. It is written to be read by someone who has seen none of the rest of
this repository.

Everything that concerns the detector's behaviour was measured after a pre-registration
(`outputs/_solar_eval/preregistration.md`) was frozen, and that document was written before any
detector output on this sample was seen.

---

## 1. Why this was done

The pipeline publishes several roof attributes from 15 cm aerial imagery. One of them —
"does this roof carry visible solar modules" — was **withheld** before this evaluation, on the
strength of a four-case spot check recorded in `docs/phase3_visual_validation.md` §3.

That check was the honest thing to do at the time and it was not enough to conclude anything.
It had four checkable cases; the detector's verdict was **inverted on all four**, which is a
striking result and also exactly what four coin flips look like. Four cases cannot distinguish
"this detector is anti-correlated with reality" from "this detector is noisy". Worse, the check
was circular: two of the detector's thresholds — `solar_panels.max_value` and
`solar_internal_texture_min` — had been calibrated from pixel measurements taken **on two of
those same four buildings**. The held-out sample size for solar was zero.

So the abstention rested on a judgement, not a measurement. This evaluation was run to replace
the judgement with a number, in either direction: if the detector cleared a pre-registered bar it
would be a candidate for publication; if it did not, the abstention would become a measured
finding with a confidence interval attached to it.

## 2. The first attempt failed on cache coverage

The obvious sample was the roofs around the ten pinned assessment buildings, using the imagery
already committed under `data/cache/`. That failed on arithmetic before it produced a single
label. The committed cache holds **118 tiles**, fetched to cover ten buildings and nothing more.
Of 44 candidate roofs in the surrounding blocks, **18 had zero imagery** and the **median
coverage was 18%** of the roof outline.

A detector cannot be charged with a false negative on a roof whose pixels were never downloaded.
The attempt was abandoned rather than run on partial crops, and the finding is recorded here
because "we had to fetch new imagery to have anything to measure" is a real constraint on what
an offline, cache-backed pipeline can validate about itself.

## 3. The sample: purposive, three zones, and not representative of Vienna

Imagery and roof records were fetched for three purpose-built evaluation zones, into
`data/eval_cache/` — a separate cache that the published pipeline never reads:

| Zone | Filter | Roofs kept |
|---|---|---|
| TU Wien / Karlsplatz, 1040 | six street names in `BEZ=1040` | 61 |
| Spengergasse, 1050 | `STR='Spengergasse'` | 44 |
| WU Wien campus, 1020 | `STR='Welthandelsplatz'` | 4 |
| **Total** | | **109** |

Eleven further roofs were dropped before any labelling as degenerate outlines — nine below 20 m²
projected area, two with `SLOPE_MEAN` above 60° (these read as wall facets, not roofs). Every
roof kept has ≥ 90% imagery coverage. None of the ten pinned assessment buildings is in the pool,
which was checked explicitly.

**This geometry pre-filter is the least auditable step in the evaluation chain.** The two
`SLOPE_MEAN > 60°` crops were not retained, so the wall-facet reading cannot now be checked, and
the main pipeline treats the same threshold as a review trigger rather than proof that a record is
not a roof. The exclusions are disclosed as a limitation; they are not evidence about detector
performance and the evaluation was not re-run to change them.

**This sample is not representative of Vienna and no statement about how common rooftop PV is in
Vienna may be derived from it.** It is deliberately enriched for institutional and campus
buildings because those carry more arrays than the ordinary stock, and it covers three zones out
of a city with 191,625 roof records. No base rate, prevalence or installed-capacity figure can
come from these labels; the enrichment guarantees such a figure would be biased upward by an
unknown amount.

**Imagery epoch.** Orthofoto 2024 Wien (`lb2024`), 15 cm true ortho, z20 ≈ 0.0995 m/px ground
sampling, read against 2025 roof records. A roof that gained or lost PV between the two epochs is
mislabelled and this cannot be detected from these sources.

## 4. The reference standard: two readings, and contradictions are dropped

The pool contained 109 roofs. **They were not all read twice**, and the exact coverage matters:

* **the assistant read all 109**, opening each crop at native resolution and zooming 2–6× wherever
  a mimic had to be adjudicated, recording a label, a confidence and a written reason for every
  roof;
* **Mohammad reviewed 103** — an 88-row queue plus 18 rows beyond it, minus three queue rows left
  blank. The remaining **6 rows carry one reading only** and are dropped as `D2_unreviewed`; they
  are never scored and are never counted as negatives.
* He saw the assistant's proposed labels, so the second reading was **anchored by the first rather
  than independent**. Agreement is inflated by that anchoring by an unknown amount.

So the scoreable reference is **90 rows: 85 strict two-reader agreements and 5 rows where the
assistant abstained and Mohammad alone resolved it**. Only those 85 carry two committed readings.

Neither reader is authoritative over the other. Where they contradict, the row is **dropped, not
adjudicated** — there is no third reading available to break a tie, and preferring one reader
would put a label into the denominator that no evidence supports.

| Rule (first match wins) | assistant | human | outcome |
|---|---|---|---|
| 1 | — | — | E1 / E2 exclusions apply first |
| 2 | committed | not reviewed | drop, `D2_unreviewed` |
| 3 | `unclear` | committed | use the human label — an abstention is not a contradiction |
| 4 | same label | same label | use it — consensus |
| 5 | `true` | `false` | drop, `D1_disagreement` |

| Outcome | n |
|---|---|
| **Scoreable** | **90** — 2 positive, 88 negative |
| — strict two-reader consensus | 85 |
| — human-resolved assistant-`unclear` (rule 3) | 5 |
| Dropped `E1` / `E2` (exclusions, §5) | 9 |
| Dropped `D1_disagreement` | 4 |
| Dropped `D2_unreviewed` | 6 |
| **Total** | **109** |

### The inter-reader disagreement rate

**Two readers looking at the same 15 cm imagery contradicted each other on 4 of the 90 rows where
both committed — 4.4%** (4 of 89, or 4.5%, after exclusions). All four are array-versus-glazing
mimic calls: raised lantern rooflights on a sedum roof, three fields of dark panes inside a bright
white lattice, a band of narrow dark strips under unbroken pale bars, a shaded strip roof.

That number is the measured floor on how well *any* label in this set is known. A nominally
"confirmed" negative carries a residual chance of being a missed array, and the five rule-3 rows
are weaker still because only one reader committed on them.

Two honest caveats on the reference standard itself:

* **The readings are not independent in the strict sense.** Mohammad reviewed with the assistant's
  proposal visible, so 4.4% is a floor, inflated toward agreement by anchoring by an unknown
  amount.
* **The four dropped contradictions were all Mohammad reading `true` against the assistant's
  `false`.** Because every dropped contradiction was a candidate *positive*, the negative set was
  untouched by the consensus rule: 88 negatives before and after. Dropping them cost the primary
  metric nothing and removed the reference standard's central weakness — that four of the six
  original positives rested on one unblinded reader overturning a specific, falsifiable reading
  with a bare `1` and no recorded note.

### The blind control sample

15 rows were drawn blind from the assistant's high-confidence `false` set with a fixed seed and
shuffled into the review queue. Mohammad did not know which. **He agreed with the assistant on
15 of 15.**

**This is not evidence of a zero error rate.** With 0 disagreements in 15 draws the
rule-of-three approximation gives 3/15 = **20%**, and the exact Clopper–Pearson 95% interval on
the disagreement rate is **0.0% – 21.8%**. The honest statement is that the two readers'
disagreement rate on easy negatives is not distinguishable from zero at n = 15 and could be as
high as roughly one in five.

That bound is far too loose to score against, which is why it was used as a *reason to refuse*
rather than a licence: no single-reader label enters any denominator, and the six unreviewed rows
were dropped rather than assumed clean. Note that 15/15 on easy negatives and 4.4% overall are
both true — the disagreements are concentrated in the hard mimic rows, which is exactly where the
`D1` drops fell.

## 5. The exclusions make the test harder, not easier

Nine rows were excluded before any detector output was seen, by opening each crop individually:

* **E1 — no roof surface inside the outline (6).** Two 49 m² diamonds over open parkland; a 24 m²
  sliver over asphalt paving; a 20 m² quadrilateral over a concrete apron *beside* a shed rather
  than over its roof; two outlines over dark inner light wells between pitched roofs.
* **E2 — the outline spans separate structures (3).** A 4,463 m² outline over an entire
  Gründerzeit block; an 8,256 m² outline covering TU blocks on *both sides of a street*; a
  1,883 m² outline enclosing a detached flat-roofed block and a pitched street wing joined only
  by landscaped garden. "Does this record have PV" has no single answer on any of them.

**All nine carry a `false` label, so the exclusions remove nine easy negatives.** That makes the
false-positive test harder, which is the intent.

Rows that were considered and deliberately **kept**: outlines over blue construction sheeting,
outlines in deep courtyard shadow, and a roof with specular sun flare across a third of it. A
detector that fires on blue sheeting or a shadow band is producing a real defect on a real street
address and must be charged for it. Difficulty is not an exclusion criterion.

## 6. What was fixed in advance, and by whom

`outputs/_solar_eval/preregistration.md` was frozen before the detector was run on a single row
of this sample. It fixes:

| Item | Value |
|---|---|
| Primary metric | false-positive rate on held-out reference negatives, with a Clopper–Pearson 95% interval, never quoted without it |
| Denominator | **35** held-out reference negatives: 33 strict two-reader agreements and 2 human-resolved assistant abstentions (`247643`, `296100`) |
| Disqualification threshold | **≥ 1 false positive disqualifies** |
| Degeneracy guard | the tuned configuration must detect `345054`; the held-out result is interpretable only if it also detects `298442` |
| Tuning objective | among configurations that detect the tune positive, minimise false positives on the 53 tune negatives — **never minimise the false-positive count alone** |
| Recall / precision | **not computed at all** |
| Split | seed `20260808`, stratified, 60% tune / 40% held-out |
| Held-out split | treated as a **one-time process step**; guarded against accidental access but not provable or enforceable by the repository |

The split gave 54 tune rows (1 positive, 53 negatives) and 36 held-out rows (1 positive, 35
negatives).

**Why zero tolerance.** At n = 35 a *perfect* score certifies only FPR ≤ 10.0% at 95% confidence;
allowing one false positive weakens that to 14.9%. Against 191,625 city-wide roof records, a
14.9% upper bound permits up to ~28,000 addresses carrying a visibly wrong claim that a customer
can walk to and check. Zero tolerance here is as much a consequence of the sample size as a
product choice, and it is recorded as such: a larger negative sample would justify a threshold
expressed as a bound rather than as a count.

**Two rules in that document were required by Mohammad and are the reason the result below can be
trusted.** They are recorded as his because a pre-registration that hides who caught what is not
much of a record.

1. **The degeneracy guard.** As first drafted, the metric was "minimise false positives" — which
   is optimised by a configuration that never fires at all. That would have scored a clean 0/35,
   cleared the threshold and been reported as a pass, while detecting nothing. The guard makes a
   never-fires configuration a disqualification regardless of its false-positive rate, and
   requires the never-fires baseline to be printed next to every result so a reader can see that
   0/35 is trivially achievable.
2. **The out-of-sample positive check.** With one positive in the tune split, tuning to make that
   roof fire is overfitting by construction, and no wording fixes it. The check runs the tuned
   configuration on `vie-swv-001` and `vie-swv-003` — two roofs in a different zone, in neither
   split, that reviewers read as carrying a visible array. It is advisory: it cannot make a
   failing configuration pass or a passing one fail; it changes the interpretation, not the
   verdict. It is also **partly circular**, because `solar_internal_texture_min` was originally
   calibrated on those same two roofs.

## 7. Tuning, on the tune split only

Six parameters were searched, and nothing else — no detector logic, no other attribute, no
schema, no threshold outside this list. Parameters were varied through the `image.tuning`
configuration hook that `attributes.param()` already prefers over `ATTRIBUTE_PARAMS`, so
**not one byte of `src/` changed at any point in this evaluation** and
`algorithm_parameters_hash` could not move by accident.

| Parameter | Shipped | Values searched |
|---|---|---|
| `solar_rectangularity_min` | 0.55 | 0.15 – 0.65, 12 values |
| `solar_periodicity_min` | 0.15 | 0.05 – 0.30, 8 values |
| `solar_internal_texture_min` | 25.0 | 15 – 45, 7 values |
| `solar_orientation_tolerance_deg` | 15.0 | 5 – 30, 6 values |
| `solar_open_m` | 0.3 | 0.2 – 0.5, 4 values |
| `solar_close_m` | 0.5 | 0.3 – 1.0, 4 values |

**64,512 configurations** were evaluated on the 54 tune rows. The held-out split was not read,
scored or inspected during this phase. The scoring harness requires an explicit flag for every
path that includes held-out rows, including `scoreable`; this prevents accidental access but
cannot prove that a human invoked the command only once.

The search prunes with a fast re-implementation of the detector's decision stage, so the four
filter parameters can be swept in arithmetic over components extracted once per morphology
setting. Nothing was decided on that surrogate: the winning configuration and 25 randomly chosen
others were re-run through the real `observe_solar_panels`, and **all 1,404 verdicts matched**.

### Reference lines, recorded before the search

| Configuration | Tune-split false positives | Detects `345054` | `vie-swv-001` | `vie-swv-003` |
|---|---|---|---|---|
| **Never fires** (degenerate) | 0 of 53 | no | no | no |
| **Shipped `ATTRIBUTE_PARAMS`** | **8 of 53** | **no** | no | no |

The shipped detector fails the degeneracy guard on its own: it fires on eight reference-negative
roofs *and* misses the tune positive. This re-measurement also reproduces
`docs/phase3_visual_validation.md` §3 exactly — `false` on eight of the ten pinned buildings and
`true` on `vie-swv-006` and `vie-swv-010` — which is a check on the harness, not on the detector.

### What the search found

**30,660 of 64,512 configurations detect the tune positive. 926 of those also produce zero false
positives on the 53 tune negatives.** Separately, **7,694 configurations score zero false
positives *without* detecting the positive** — the degenerate solutions the guard exists to
exclude. They outnumber the genuine zero-false-positive configurations more than eight to one,
which is the clearest possible demonstration that a false-positive threshold alone proves
nothing.

The pre-registered rule selects, among the 926, the configuration closest to the shipped
parameters:

| Parameter | Shipped | **Tuned** |
|---|---|---|
| `solar_rectangularity_min` | 0.55 | **0.40** |
| `solar_internal_texture_min` | 25.0 | **40.0** |
| the other four | unchanged | unchanged |

On the tune split: **1 true positive, 0 false positives, 53 true negatives, 0 false negatives.**
A perfect score on 54 rows.

### Sensitivity — the perfect tune score is a knife edge

Each parameter was moved ±5% on its own and the tune split re-measured with the real detector.
**2 of the 12 moves change the result:**

| Move | Effect |
|---|---|
| `solar_rectangularity_min` 0.40 → **0.42** | **loses the tune positive** |
| `solar_internal_texture_min` 40.0 → **38.0** | **gains a false positive** |

The reason is visible in the diagnostics. The tuned configuration detects `345054` because a
70.1 m² cluster has rectangularity **0.401** against a threshold of **0.400** — a margin of one
part in four hundred. It is a fit to one cluster on one roof.

Two further findings from the same check:

* **±5% on `solar_open_m` or `solar_close_m` is a no-op.** Both are morphological kernel radii
  converted to a whole number of pixels, and at 0.0995 m/px a 5% change does not cross a pixel
  boundary. Those two parameters are quantised at this ground sampling distance and cannot be
  tuned finely, which is worth knowing independently of this result.
* **A stable region does exist.** Of the 926 zero-false-positive configurations, **70 survive
  every single-parameter ±5% move.** The pre-registered tie-break — closest to the shipped
  parameters — picked a fragile one. That rule was frozen before any result was seen and it was
  not changed afterwards; switching selection rules once the winner looks bad is precisely what
  pre-registration exists to prevent. The existence of the robust set is reported here, and it is
  the natural starting point for anyone who picks this up.

## 8. The held-out result, treated as the one-time score

| | Reference `true` | Reference `false` |
|---|---|---|
| **Detector fires** | 1 | **1** |
| **Detector does not fire** | 0 | 34 |

**False-positive rate: 1 of 35 = 2.9%, Clopper–Pearson 95% interval [0.07%, 14.9%].**

**Degeneracy guard: `298442` detected — the result is interpretable.** The configuration is not
inert.

**The pre-registered threshold is not met.** One false positive disqualifies. Per §6 of the
pre-registration this is failure branch **F2**: *the approach discriminates, but not well enough
at 15 cm* — a precision problem rather than a design impossibility.

For comparison, printed here because the pre-registration requires it: **a configuration that
never fires scores 0 of 35 on the same negatives, Clopper–Pearson 95% [0.0%, 10.0%] — a better
number than the tuned detector achieved, while detecting nothing.**

### The mimic breakdown

One false positive, and its class is unambiguous:

| OBJECTID | Address | Mimic class | What the detector accepted |
|---|---|---|---|
| **295076** | 04., Gußhausstraße 17 | **shadow** | a single 12.6 m² elongated sliver lying along the south-east eave, on the boundary between the sunlit terracotta roof and the deep courtyard shadow. Internal texture 48.0, periodicity 0.27, coverage 4.0% |

Both readers labelled this roof `false` with high confidence; the assistant's recorded reason
reads *"Brown-terracotta pitched roof over three wings with a pale grey metal cross-gable and two
chimneys, fully sunlit; no dark rectangular field."* The 137.7 m² cluster that actually
corresponds to roof fabric was rejected as `interior_too_smooth`. What survived was the shadow
edge.

The tuned configuration fired on **no** row among the 19 dropped rows — none of the four
`D1` contradictions, none of the exclusions, none of the unreviewed rows.

### The out-of-sample positive check — advisory, and it is damning

| Roof | Reviewer read | Shipped detector | **Tuned detector** |
|---|---|---|---|
| `vie-swv-001` | visible array | `false` | **`false`** |
| `vie-swv-003` | visible array | `false` | **`false`** |

Per §2.2 of the pre-registration, a configuration that fires on `345054` but on neither 001 nor
003 is **reported as probably overfitted to the tune positive**, and that reading is written into
the conclusion. It is written here.

The mechanism is exact, and it is worse than a miss:

* On **`vie-swv-001`** the real 932.8 m² array *is* found as a single cluster. Under the shipped
  parameters it was rejected as `not_rectangular_enough` (0.455 < 0.55). Lowering
  `solar_rectangularity_min` to 0.40 would have admitted it — but raising
  `solar_internal_texture_min` to 40.0 rejects it again as `interior_too_smooth`, because the
  array's interior texture is **34.1**. The tuning moved a real array from one rejection reason
  to the other.
* On **`vie-swv-003`** the 287.8 m² array cluster (texture **27.3**) was *accepted* under the
  shipped parameters and is now rejected as `interior_too_smooth`.
* On **`vie-swv-009`**, where reviewers read *no* visible array, the tuned configuration fires on
  a 19.1 m² sliver in the shadowed light-well between two roof masses — texture **40.8**, just
  above the new threshold, admitted only because `solar_rectangularity_min` fell to 0.40. **A
  false positive the shipped configuration did not have**, of the same shadow class as 295076.

### The finding underneath the numbers

The internal-texture gate points the wrong way, and the evaluation data says so numerically:

| Cluster | Internal texture |
|---|---|
| Real array, `vie-swv-001` | 34.1 |
| Real array, `vie-swv-003` | 27.3 |
| Shadow sliver, 295076 (false positive) | 48.0 |
| Shadow sliver, `vie-swv-009` (false positive) | 40.8 |
| Shadow/edge clusters accepted on `345054` | 45.5 – 80.9 |

The gate was introduced to reject shadow, and its docstring records that it was calibrated
against *large soft cast shadows*, which score 8–22 and are genuinely smooth. But the shadows
that survive to become false positives are **thin slivers at high-contrast roof edges**, and the
mean-absolute-Laplacian statistic measures a thin cluster's boundary rather than its interior.
On those, "internal texture" reads 40–80 — higher than a real module field, whose interior at
15 cm is comparatively uniform. Raising the threshold therefore selects *against* arrays and *for*
edge shadow. It cannot be fixed by moving the number in either direction; it needs a statistic
that is not size-dependent, or an explicit rejection of clusters whose area-to-perimeter ratio
marks them as slivers.

That is the single most useful thing this exercise produced, and no count of false positives
would have revealed it.

## 9. Decision

The threshold is not met, so the pre-registered failure branch applies:

1. **`ATTRIBUTE_PARAMS` is unchanged.** It never changed: the search ran entirely through the
   `image.tuning` configuration hook, so there was nothing to revert. `git diff` on
   `src/propx_roofs/attributes/__init__.py` is empty, and `algorithm_parameters_hash` is
   `cd96abe1b9118c3f`, identical to the value in the published `outputs/roof_attributes.json`.
2. **The solar attribute continues to emit `null` / `unavailable` / `null`**, with the raw
   diagnostics — `withheld_detector_verdict`, candidate clusters, coverage fractions — retained
   in the output for a future validation. **Nothing about the published output changes.**
3. **The abstention is now a measured finding rather than a judgement**: measured on 35
   reference-negative roofs — 33 strict two-reader agreements and 2 human-resolved assistant
   abstentions — with 1 false positive,
   Clopper–Pearson 95% [0.07%, 14.9%], failing a threshold fixed in advance, with the failure
   traced to a specific mechanism in §8.

## 10. What this evaluation cannot measure

Stated plainly, and required to accompany any quotation of the numbers above.

* **Recall is not measured, and no recall figure exists in this document.** The reference set
  holds 2 consensus positives and the held-out split holds 1. A recall figure from one positive
  can only be 0% or 100%. The two positives function as a *degeneracy guard* — a necessary
  condition, reported as two binary facts — and passing it is a floor, not an achievement.
* **Precision is not reported as independent evidence.** At this positive count it is
  arithmetically dominated by the false-positive count and carries nothing the FPR does not.
* **Nothing about Vienna.** No base rate, no prevalence, no installed-capacity figure. Three
  purposively chosen zones, deliberately enriched, out of 191,625 records.
* **The precision measurement itself is weak.** Even a *perfect* 0/35 would have certified only
  FPR ≤ 10.0% at 95%. The measured [0.07%, 14.9%] permits, at its upper bound, roughly 28,000
  wrong claims across the city-wide layer.
* **The labels are not ground truth.** They are two model-and-human readings of the same 15 cm
  imagery the detector reads, and the two readers contradicted each other on 4.4% of rows. There
  is no independent reference: `ANLAGENLEISTUNG` is *modelled* PV potential in kWp, not installed
  capacity, so the authoritative layer cannot say whether a roof carries panels.
* **The out-of-sample check is three roofs and partly circular.** It is a third opinion, not an
  independent test set.
* **A 2024/2025 epoch mismatch is undetectable** from these sources.

## 11. What would actually settle it

1. **A reference standard that is not a reading of the same image.** Installation registry data,
   an oblique or street-level pass, or a second imagery epoch. Everything in §10 flows from the
   labels and the detector sharing one source of truth.
2. **More positives.** Two is enough to catch a detector that does nothing and not enough for
   anything else. Recall needs a positive-enriched sample built on purpose, of order 100 arrays.
3. **Fix the sliver problem before tuning anything again.** §8 names a specific, testable defect
   in the internal-texture gate. Re-tuning the six parameters without addressing it is moving a
   threshold that points the wrong way.
4. **Start from the robust set, not the fragile winner.** 70 of the 926 zero-false-positive
   configurations survive every ±5% perturbation. Ten of them are listed under
   `robustness_census.robust_examples` in `outputs/_solar_eval/phase4_report.json`, and
   `phase4_verify.py` regenerates all 70.

---

## Auditing the recorded evaluation

**This evaluation cannot be re-run end to end from a clean clone, and is not submitted as if it
could be.** The imagery it was measured on — `data/eval_cache/` for the three evaluation zones,
plus the crops, contact sheets and review sheets — is deliberately not committed: roughly 130 MB
of JPEG tiles and PNGs, fetched from the live City of Vienna services for this evaluation alone.
No committed tool rebuilds that cache. What is committed is the evidence: the labels, the frozen
rules, the scripts and every result file.

This limitation applies **only to this supplementary evaluation**. The main rooftop pipeline runs
offline from the committed cache with no missing artefact; `make verify-repro` then checks
byte-for-byte reproduction in the **currently installed environment**, not reproducibility in
general.

**What a reviewer can audit from the committed evidence alone**, with no imagery and no network:

| Artefact | What can be checked against it |
|---|---|
| `labels.csv` | every assistant reading, the 103 recorded human readings (with six blanks visible), the consensus rule's outcome per row, the exclusions |
| `splits.json` | the split membership and seed, and that no row appears in both splits |
| `preregistration.md` | the rules, the recorded file hashes, and that the reported metric is the one that was fixed |
| `PHASE3_REPORT.md` | how the reference was built and repaired |
| `phase4_search.json`, `phase4_report.json` | the search space, the winner, the sensitivity and robustness census |
| `phase5_report.json` | the held-out result and every diagnostic behind it |
| `build_reference.py` | that the consensus rule as executed matches the rule as described |

The Clopper–Pearson intervals, the confusion matrix, the disagreement rate and the consensus rule
can all be recomputed from `labels.csv` and `splits.json` by hand. That is the intended audit path.

**Scripts preserved as an executable record.** These document exactly what was run. Only
`build_reference.py` runs from the committed evidence alone; the other three read the evaluation
imagery and therefore require the original local `data/eval_cache/`:

| Script | Runs from a clean clone? |
|---|---|
| `build_reference.py` — consensus rule and split | **yes**, reads only `labels.csv` |
| `search.py` — the six-parameter search | no, needs `data/eval_cache/` |
| `phase4_verify.py` — surrogate check, sensitivity, robustness | no, needs `data/eval_cache/` |
| `phase5_score.py` — the held-out scoring | no, needs `data/eval_cache/` |

`score.py` requires `--score-the-held-out-split-once` on every path that touches held-out rows,
including `--split scoreable`. The flag prevents accidental access. It **cannot prove or enforce**
that the command was invoked only once: "scored once" is process discipline, recorded here as a
claim about how the work was done, not as something this repository can demonstrate.

| Artefact | What it holds |
|---|---|
| `labels.csv` | 109 rows: assistant reading on all, human reading on 103, `reference_label`, `drop_reason`, the exclusions |
| `splits.json` | seed `20260808`, split membership, the disagreement rate |
| `preregistration.md` | the frozen rules |
| `PHASE3_REPORT.md` | the reference-standard construction and its repair history |
| `phase4_search.json`, `phase4_report.json` | the search, the sensitivity, the robustness census |
| `phase5_report.json` | the held-out result and every diagnostic behind it |
