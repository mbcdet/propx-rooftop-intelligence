# Pre-registration — rooftop PV detector evaluation

**Status: APPROVED by Mohammad and FROZEN. This document is fixed.**

Approved after two amendments he required and this assistant did not propose: the **degeneracy
guard** in §2.2, which closes the hole whereby a detector that never fires would have scored a
clean 0/35 and been reported as a pass, and the **out-of-sample positive check** in §2.2, which
is the only positive evidence in this exercise that no Phase 4 decision can touch. Both are
recorded here as his, because a pre-registration that hides who caught what is not much of a
record. Nothing below may be edited on the strength of a result; if something in it turns out to
be wrong, the failure is reported and the rule stands as written.

Written **before** the detector was run. At the time of writing the detector has not been
executed against any row of this dataset, `ATTRIBUTE_PARAMS` has not been touched, and no
detector output of any kind has been seen. Every number below that concerns the detector is a
rule, not a result.

Inputs fixed by this document:

| Artefact | What it fixes |
|---|---|
| `labels.csv` | `reference_label` and `drop_reason` — the consensus labels, the exclusions, the review provenance |
| `splits.json` | seed `20260808`, the tune / held-out membership by OBJECTID |
| `build_reference.py` | the consensus rule as executable code, so §0 is auditable rather than described |
| this file | the metric, the threshold, the reporting, both branches |

---

## 0. The reference standard is a two-reader consensus

Two independent readings exist for every row: the assistant's full-resolution pass-2 reading
of each crop, and Mohammad's review. **Neither reader is authoritative over the other.** Where
they contradict each other the row is dropped, not adjudicated — there is no third reading that
could break the tie, and inventing one by preferring a reader would put a label into the
denominator that no evidence supports.

The rule, applied in this order by `build_reference.py`; the first line a row meets decides it:

| # | assistant | human | outcome |
|---|---|---|---|
| 1 | — | — | E1 / E2 exclusions win first (`drop_reason` = `E1` / `E2`) |
| 2 | committed | not reviewed | drop, `D2_unreviewed` |
| 3 | `unclear` | committed `true`/`false` | **use the human label.** One reader abstained and the other resolved it; an abstention is not a contradiction. Counted and reported separately below |
| 4 | committed, same label | committed, same label | **use it** — consensus |
| 5 | `true` | `false` (or the reverse) | drop, `D1_disagreement` |

### What the rule does to the set

| Outcome | n | OBJECTIDs |
|---|---|---|
| Scoreable (`reference_label` non-empty) | **90** | — |
| — of which strict two-reader consensus | 85 | — |
| — of which human-resolved assistant-`unclear` (rule 3) | 5 | 247643, 268882, 296100, 303044, 320598 |
| Dropped `E1` — no roof surface inside the outline | 6 | 219006, 269353, 355040, 355731, 355737, 372507 |
| Dropped `E2` — outline spans separate structures | 3 | 202631, 258556, 264023 |
| Dropped `D1_disagreement` | 4 | 248105, 268929, 355083, 368679 |
| Dropped `D2_unreviewed` | 6 | 213848, 216986, 217211, 217897, 222220, 225670 |
| **Total rows** | **109** | |

**Consensus positives: 2** — `298442` (05., Spengergasse 44) and `345054` (05., Spengergasse 29),
both read `true` with high confidence by both readers. **Consensus negatives: 88.**

All four `D1` contradictions were rows where Mohammad read `true` and the assistant read `false`,
and all four are array-versus-glazing mimic calls (Phase 3 report §5). Because every dropped
contradiction was a candidate *positive*, **the negative set is unchanged at 88 rows** — the
consensus rule costs the primary metric nothing at all. What it removes is the previous
reference standard's central weakness: four of six positives rested on a single unblinded
reviewer overturning a specific, falsifiable assistant reading with a bare `1` and no recorded
note.

### Inter-reader disagreement rate

A property of the reference standard, reported wherever it is quoted:

| Frame | Contradictions | Rows where both readers committed | Rate |
|---|---|---|---|
| All rows | 4 | 90 | **4.4%** |
| After the E1/E2 exclusions | 4 | 89 | **4.5%** |

Two readers working from the same 15 cm imagery disagree outright on about one roof in
twenty-two, and every disagreement is a mimic call. That is the measured floor on how well
*any* label in this set is known, and it is not smoothed over: it means a nominally
"confirmed" negative in the denominator carries a residual chance of being a missed array.
The 5 rule-3 rows are weaker still — they carry one committed reader rather than two.

---

## 1. Primary metric

**False-positive rate on confirmed-negative roofs.**

- **Denominator** — held-out rows with `reference_label == false`.
  From `splits.json` (seed `20260808`, stratified, 60/40): **n = 35**.
- **Numerator** — of those 35, the ones the detector calls positive.
- **Reported as** the point estimate plus a **Clopper–Pearson 95% interval**, always together.
  The point estimate is never quoted on its own.

| Split | Total | Positives | Negatives |
|---|---|---|---|
| Tune | 54 | 1 — `345054` | 53 |
| Held-out | 36 | 1 — `298442` | **35** |

The denominator is 35 both before and after the consensus rule, for the reason given in §0.

Nothing else is the primary metric. The tune split may be looked at freely while tuning; the
held-out 36 rows are scored **once**. If they are scored a second time after any parameter
change, the result of the second scoring is not a held-out result and must be labelled as such.

Rows with an empty `reference_label` are not in either split and are not scored. An empty cell
is not a negative, and a dropped contradiction is not a negative either.

## 2. Disqualification threshold, degeneracy guard, and the tuning objective

A false-positive threshold on its own is a hole, not a bar. **A detector that never fires scores
0 false positives on 35 negatives, clears the threshold in §2.1, and detects nothing.** It is the
current abstention wearing a rosette. §2.2 – §2.4 close that hole, and they are as binding as
§2.1: all of it is one rule and no part of it may be quoted without the rest.

### 2.1 The false-positive threshold

> **The detector is disqualified unless it produces zero false positives on the 35 held-out
> confirmed negatives.** One false positive disqualifies.

This is chosen now, before any output is seen, and here is the arithmetic behind it.

| False positives / 35 | Point FPR | Clopper–Pearson 95% |
|---|---|---|
| 0 | 0.0% | 0.0% – **10.0%** |
| 1 | 2.9% | 0.07% – **14.9%** |
| 2 | 5.7% | 0.70% – **19.2%** |
| 3 | 8.6% | 1.80% – **23.1%** |

With 35 negatives, a perfect score still only certifies *FPR ≤ 10.0%* at 95% confidence. That is
the **best** statement this sample can make. Allowing a single false positive weakens the
certified bound to 14.9%.

The product cost sets which of those is acceptable. A false "this roof has PV" is not a subtle
quality issue: it is a wrong factual claim attached to a real street address that the customer
can walk to and check. The roof layer has **191,625** records city-wide. An FPR whose upper bound
is 14.9% permits up to ~28,000 addresses carrying a visibly wrong claim; at 10.0% it is ~19,000.
Neither is comfortable, which is the honest reading of a 35-row denominator — but 10.0% is the
floor this sample can reach, and there is no defensible reason to pre-register anything looser
than the floor when the looser number buys nothing except a higher chance of shipping.

The counter-argument, stated so it is on the record: this sample is deliberately enriched for
mimics — patent glazing, standing-seam metal, rooflight rows and monitors, construction sheeting,
shadow bands, specular flare — so a zero-defect bar is a hard test. It is nevertheless a fair
one. Those mimics are not exotic; they are ordinary Vienna roof fabric, and a city-wide run meets
them constantly. A detector that cannot clear them here will not clear them at scale.

**Zero tolerance is a consequence of the sample size as much as a product choice, and it is
recorded as such.** A larger negative sample would justify a threshold expressed as a bound
rather than as a count. This one does not.

### 2.2 Degeneracy guard — a configuration that does not fire is not a detector

> **A configuration that fails to fire on the consensus positives is disqualified regardless of
> its false-positive rate.**

Concretely, and fixed now:

| Requirement | Consequence if it fails |
|---|---|
| The tuned configuration **must detect `345054`** (tune positive) | It is not a candidate. It may not be carried into Phase 5, and it may not be scored on the held-out split |
| Its held-out result is interpretable **only if it also detects `298442`** (held-out positive) | A 0/35 held-out result from a configuration that missed `298442` is **reported as uninterpretable**, never as a pass |

A never-fires configuration is not a passing detector and **must never be reported as one** —
not in this file, not in `docs/solar_evaluation.md`, not in the README, not in a summary table.
Where a result is reported, the guard is reported in the same sentence.

This makes the two consensus positives a **gate**, not a metric. Their role is to falsify the
degenerate solution, and one roof is enough to do that: a detector that cannot fire on an
unambiguous flush-mounted array has failed a necessary condition. It is not enough to *pass*
anything — see §3.

**The weakness of the guard, stated now rather than discovered later.** The tune gate rests on a
single roof. Tuning to make one specific roof fire is overfitting by construction, and any
configuration that clears it has been fitted to `345054` in a way that will not generalise. That
is a limitation of the sample, it is unavoidable at 2 positives, and it is reported next to the
result every time. The guard is therefore a floor on degeneracy, not evidence of recall.

#### Out-of-sample positive check — advisory, and it is the answer to that weakness

After Phase 5, the tuned configuration is **also run on `vie-swv-001` and `vie-swv-003`**, two of
the ten pinned assessment buildings, which `docs/phase3_visual_validation.md` §3 records as roofs
where reviewers read a **visible** array. They sit in a different zone, in neither split, and no
parameter is tuned on them in Phase 4.

| Property | Value |
|---|---|
| Reported as | **two binary facts** — fires on `vie-swv-001`: yes/no; on `vie-swv-003`: yes/no |
| Never | summed into "2 of 2", called recall, or given a percentage or an interval |
| Status | **advisory.** It cannot make a failing configuration pass and cannot make a passing one fail. It changes the interpretation, not the verdict |

Its role is to separate two readings that the tune gate alone cannot distinguish:

| Result | Written into the conclusion as |
|---|---|
| Fires on `345054` but on **neither** 001 nor 003 | **Probably overfitted to the tune positive.** Stated in those words in `docs/solar_evaluation.md` |
| Fires on `345054` and on **one** of the two | Weak and ambiguous; reported as such, with which one and why the other differs |
| Fires on **all three** | Meaningfully less likely to be a single-roof fit. **Still not evidence of recall** — three roofs is not a population |

**This check is weaker than it looks, and the reason is on the record.** Reviewer C's circularity
warning in `docs/phase3_visual_validation.md` §3 states that `solar_panels.max_value` (145) and
**`solar_internal_texture_min` (25.0) were set from pixel measurements taken on `vie-swv-001` and
`vie-swv-003`**. `solar_internal_texture_min` is one of the six parameters Phase 4 searches. These
two roofs are therefore *out-of-sample with respect to this evaluation's splits* but **not virgin
data with respect to the detector**: part of the shipped configuration was calibrated on them
before this exercise began. A firing on 001 or 003 is consequently partly circular, and it is
reported with that sentence attached. It is still worth having — it is the only positive evidence
in this exercise that no Phase 4 decision can touch — but it is a third opinion, not an
independent test set.

### 2.3 The tuning objective — stated explicitly, not left implied

> **Do not minimise the false-positive count alone.** Search for configurations that **detect the
> tune positive `345054`**, and among only those, minimise false positives on the 53 tune
> negatives. Ties are broken toward the configuration closest to the shipped parameters.

If **no** configuration in the six-parameter space of Phase 4 detects `345054` at any acceptable
false-positive count, **that is the finding**, and it is the strongest result this exercise can
produce: the detector cannot be made to work on this data by parameter choice, only by redesign.
It is reported in exactly those terms. Falling back to a never-fires configuration and reporting
its 0/35 as a pass is explicitly forbidden by §2.2.

"Acceptable" is not left to judgement after the fact. Phase 4 reports the **whole trade-off**:
for every configuration that detects `345054`, its false-positive count on the 53 tune negatives,
so the reader sees whether the positive is recovered at a cost of one negative or of forty. A
configuration that fires on the tune positive only by also firing on a large share of the tune
negatives is a detector that fires on everything — the mirror image of the degenerate case, and
equally not a detector. It is named as such rather than carried forward.

Two outcomes are therefore possible failures, and they are **not the same failure** and must not
be reported as though they were:

| Outcome | What it means |
|---|---|
| Detects the positive, but fires on ≥ 1 held-out negative | The approach discriminates, but not well enough at 15 cm. A precision problem — the mimic breakdown in §4 says whether it is fixable |
| Cannot detect the positive at any setting | Parameter choice cannot rescue it. A design problem, not a threshold problem |

### 2.4 Reference lines that must be reported alongside any result

Both are reported whatever the outcome, so that a reader can see for themselves that the
threshold alone proves nothing.

1. **The degenerate baseline.** What a never-fires configuration scores: 0 false positives out of
   35, Clopper–Pearson 95% 0.0% – 10.0%, and 0 of 2 positives detected. Printed next to the tuned
   result in every table. **0/35 is trivially achievable**, and a reader who sees only the FPR
   cannot distinguish a working detector from an inert one.
2. **The currently shipped `ATTRIBUTE_PARAMS`, scored on the tune split before any search
   begins.** The starting point: how many of the 53 tune negatives it fires on and whether it
   detects `345054`. Recorded before the first parameter is varied, so the search has a baseline
   a reader can compare against and so any improvement claimed by the search is measured against
   something rather than asserted.
3. **The shipped `ATTRIBUTE_PARAMS` on `vie-swv-001` and `vie-swv-003`**, in the same table.
   `docs/phase3_visual_validation.md` §3 already records the withheld detector verdict as
   **`false` on both**, against reviewer reads of *visible* — the detector found 001's array and
   rejected it as `not_rectangular_enough` (0.455 against a 0.55 threshold), and accepted 003's
   cluster but failed the regularity gate (periodicity 0.1331 against a 0.15 threshold). That
   `false`/`false` is the baseline the search has to beat, it is re-measured here rather than
   quoted, and both near-misses sit on parameters inside the Phase 4 search space — which is a
   reason to expect the search to move them and a reason to be suspicious when it does.

## 3. Recall and precision will not be computed at all

**With 2 consensus positives in the entire reference set and 1 in the held-out split, no recall
or precision figure will be computed — not as a percentage, not with an interval, not with a
caveat.** A recall figure from one held-out positive can only be 0% or 100%; from all 2
positives it can only be 0%, 50% or 100%. Numbers like that are not estimates, they are coin
flips wearing a percent sign, and once "50% recall" exists in a document it will be quoted
without its interval. The only defence is not to produce it.

Precision is excluded for the same reason compounded: at this positive count precision is
arithmetically dominated by the false-positive count, so it is the primary metric restated
and carries nothing the FPR does not already carry.

**The degeneracy guard in §2.2 is not recall and may not be reported as recall.** The distinction
is exact and it matters:

| | What it is | How it is reported |
|---|---|---|
| The guard (§2.2) | A **necessary condition**. Two named roofs, `345054` and `298442`, each detected or not | A raw binary fact per OBJECTID: *"detected `345054`: yes/no"*. Never a fraction, never "1 of 2", never a percentage |
| Recall | A **rate** estimated over a population of positives | **Not computed.** There is no population here — there are two roofs |

Passing the guard is a floor, not an achievement: it says the configuration is not inert. It
says nothing about how many real arrays the detector would find in Vienna, and no reader may
infer that it does.

**So what this evaluation measures is exactly this: false-positive behaviour on 35 confirmed-
negative roofs, conditional on the detector not being inert.** Whether the detector finds real
arrays at any useful rate is outside what this sample can answer. That is a limitation of the
sample, not a finding about the detector, and every quotation of these results must carry it.

**No base rate may be derived from this sample.** See §8.

## 4. Secondary reporting — raw counts only, explicitly not estimates

Every item in this section is reported as **"k of n"** with the words *raw count, not an
estimate* attached. No percentages, no intervals, and nothing in this section is a metric.

1. **The degeneracy guard result** (§2.2): `345054` detected yes/no, `298442` detected yes/no,
   as two binary facts, never summed into a fraction. Two rows is an anecdote and is reported as
   one.
2. **The two reference lines from §2.4** — the degenerate never-fires baseline, and the shipped
   parameters on the tune split before the search — in the same table as any tuned result.
3. **The 4 dropped `D1` contradictions**, listed separately with what the detector did on each.
   They score nothing and change no decision. They are reported because a detector that fires on
   exactly the rows two careful readers could not agree about is telling us something about where
   the boundary genuinely is.
4. **Every false positive, broken down by mimic class**, one row each, with OBJECTID, address and
   the crop:
   - glazing (patent glazing, rooflights, lanterns, glazed canopies)
   - metal roofing (standing seam, ribbed sheet, valleys)
   - construction sheeting
   - shadow (cast bands, deep courtyard shadow)
   - neighbour bleed (the feature is real but lies outside the outline)
   - plant (HVAC units, vent boxes, ducting, ballast paving)

The mimic breakdown is the part of this report that carries engineering value. A count says
whether the detector passed; the breakdown says whether a failure is a fixable class defect (all
false positives are glazing → a contrast-polarity rule) or a diffuse one (spread across six
classes → the approach is wrong). It is reported whether the threshold is met or not.

## 5. The blind control sample

15 rows were drawn blind from the high-confidence `false` set with seed `20260806` and hidden in
the review queue. Mohammad did not know which. He agreed with the assistant on **15 of 15**.

**This is not evidence of a zero error rate.** With 0 disagreements in 15, the Clopper–Pearson
95% interval on the disagreement rate is **0.0% – 21.8%**; the rule-of-three approximation gives
3/15 = 20%. The honest statement is: *the two readers' disagreement rate on high-confidence
negatives is not distinguishable from zero at n = 15, and could be as high as roughly one in
five.*

Two consequences, both binding:

1. **No single-reader label enters any denominator.** Every scoreable row carries both readings,
   except the 5 rule-3 rows in §0 where one reader explicitly abstained. An upper bound of 21.8%
   is far too loose to license scoring against one reader alone.
2. **Unreviewed rows are not treated as clean.** 6 rows were never reviewed (`213848`, `216986`,
   `217211`, `217897`, `222220`, `225670`). They are dropped as `D2_unreviewed`: not scored, not
   counted as negatives, not assumed correct. Dropping them shrinks the denominator, and that
   shrinkage is already reflected in the intervals in §2 — it is not an additional hidden
   allowance.

Note the tension between this section and §0 honestly: the control says the two readers agree
15/15 on easy negatives, while the whole-set disagreement rate is 4.4%. Both are true, and
together they say the disagreements are concentrated in the hard mimic rows — which is exactly
where the `D1` drops fell.

## 6. The failure branch — written before the result is known

There are now **three** ways to reach it, and the write-up must name which one occurred:

| # | Trigger | Written up as |
|---|---|---|
| F1 | No configuration in the Phase 4 space detects `345054` (§2.3) | *The detector cannot be made to work on this data by parameter choice.* The strongest result available: a design finding, not a threshold miss. Phase 5 is not run — there is no candidate to score |
| F2 | The tuned configuration detects `345054` but fires on ≥ 1 held-out confirmed negative | *The approach discriminates but not well enough at 15 cm.* A precision finding; the §4 mimic breakdown says whether it is a fixable class defect |
| F3 | 0 false positives on 35, but the configuration failed the guard on `298442` | **Uninterpretable, not a pass** (§2.2). Reported as a degenerate or near-degenerate result and never as a clean 0/35 |

In all three cases:

1. `ATTRIBUTE_PARAMS` is reverted **byte-identical** to its current committed state, verified by
   diff and by an unchanged `algorithm_parameters_hash`.
2. The solar attribute continues to emit `null` / `unavailable` / `null`.
3. **The measured numbers become the published reason for the abstention.** The abstention is not
   documented as "not attempted" or "out of scope". It is documented as: measured on 35
   confirmed-negative roofs, k false positives, Clopper–Pearson 95% interval [lo, hi], the guard
   result on both named positives, and the mimic breakdown from §4 naming what it fired on.

An abstention backed by a measurement is a stronger artefact than a shipped attribute backed by
none, and it is the intended outcome of this evaluation if the number does not clear.

## 7. The success branch

Success requires **both** conditions, and either one alone is not success:

1. Zero false positives on the 35 held-out confirmed negatives (§2.1), **and**
2. the degeneracy guard passed — `345054` **and** `298442` both detected (§2.2).

Even then, nothing is published automatically. Changing `ATTRIBUTE_PARAMS` moves
`algorithm_parameters_hash`, which regenerates **every output in the submission** — not only the
solar attribute. That is a submission-wide change triggered by a 35-row test that has passed a
two-roof degeneracy floor and measured no recall at all (§3), and it requires **Mohammad's
explicit, separate decision**, made after seeing the §4 breakdown, not implied by the threshold
being met.

Until that decision, the parameters stay as they are.

## 8. Sample honesty

Stated in the same terms wherever any number from this evaluation is quoted:

- **Purposive, not random.** Deliberately enriched for institutional and campus buildings because
  they carry more rooftop arrays than the ordinary stock.
- **Three zones only** — Spengergasse (1050), TU Wien / Karlsplatz (1040), WU Wien campus (1020).
  109 roof records, 90 of them scoreable.
- **Not representative of Vienna.** No statement about how common rooftop PV is in Vienna may be
  derived from it, and no base rate, prevalence or city-wide installed-capacity figure may be
  computed from these labels — the enrichment guarantees such a figure would be biased upward by
  an unknown amount.
- **Reference standard is a two-reader consensus, and the two readers were not independent in
  the strict sense**: Mohammad reviewed with the assistant's proposal visible, so agreement is
  inflated by anchoring to an unknown degree. Contradictions are dropped rather than resolved,
  which trades sample size for defensibility. The measured disagreement rate is 4.4% (§0).
- **Only false-positive behaviour is measured**, and only conditional on the detector not being
  inert. 2 consensus positives support no recall figure and none is computed (§3); they serve
  only as the degeneracy guard (§2.2), and clearing a two-roof floor is not evidence of recall.
  A configuration tuned to fire on one named roof is overfitted to that roof by construction.
- **Imagery epoch.** Orthofoto 2024 Wien (`lb2024`), 15 cm true ortho, z20 ≈ 0.0995 m/px, against
  2025 roof records. A roof that gained or lost PV between the two epochs is mislabelled, and
  this cannot be detected from these sources.

---

## Fixed at approval

| Item | Value |
|---|---|
| `labels.csv` sha256 at freeze | `5e897da80114f1bc1deae9311dd667dc17733e68bdff8e8b2aa1268306a0b350` |
| `splits.json` sha256 at freeze | `3d67c841a7dc1c4694f9492d6f1f1c07b4bb4c97266aa67f337bf3aead38ff9f` |
| Reference standard | two-reader consensus; contradictions dropped, not adjudicated |
| Scoreable rows | 90 of 109 (2 positive, 88 negative) |
| Inter-reader disagreement | 4 of 90 rows where both committed = 4.4% |
| Primary metric | FPR on held-out confirmed negatives, Clopper–Pearson 95% |
| Denominator | 35 |
| Disqualification threshold | ≥ 1 false positive |
| Degeneracy guard | must detect `345054`; held-out result interpretable only if `298442` also detected. A never-fires configuration is never a pass |
| Tuning objective | among configurations detecting `345054`, minimise false positives on the 53 tune negatives. Never minimise FPR alone |
| Out-of-sample positive check | `vie-swv-001`, `vie-swv-003` after Phase 5. Two binary facts, advisory only, partly circular (§2.2) |
| Reference lines reported | never-fires baseline (0/35, 0 positives); shipped `ATTRIBUTE_PARAMS` on the tune split; shipped `ATTRIBUTE_PARAMS` on 001/003 (known `false`/`false`) |
| Recall / precision | not computed, at all. The guard is not recall |
| Split seed | 20260808 (supersedes 20260807) |
| Control seed | 20260806 |
| Control result | 15/15 agreement; disagreement rate 95% CI 0.0% – 21.8% |
| Held-out scored | once |
| Failure branch | revert `ATTRIBUTE_PARAMS` byte-identical, publish the numbers as the reason |
| Success branch | no publication without an explicit separate decision |
