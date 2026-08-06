# Phase 3 report — repair, exclusions, control sample, split

Written before any detector output was seen. The detector has not been run.
`ATTRIBUTE_PARAMS` was not touched. Nothing was tuned.

---

## 1. What Numbers damaged, and what was repaired

The export was recovered as `outputs/_solar_eval/labels - labels.csv.csv` and is kept
byte-identical as **`labels_numbers_export.csv`** so the repair is auditable. The restored file
is `labels.csv`.

The repair baseline is a byte-exact rebuild of the pre-Numbers `labels.csv` from its actual
sources — the 20 pass-2 batch judgements, `calibration.json` and `labels_pass1.csv`, replaying
`build_labels.py` with seed `20260806`. The rebuild reproduced the queue order and the 15 control
OBJECTIDs exactly, which confirms it is the same file presented for human review.

Three kinds of damage, all repaired:

| # | Damage | Extent | Repair |
|---|---|---|---|
| 1 | `assistant_label` coerced to booleans | `true`→`TRUE` ×2, `false`→`FALSE` ×94 (96 cells). `unclear` ×13 untouched | lower-cased back to `true`/`false`/`unclear` |
| 2 | **`slope_mean_deg` lost its `.0`** — not previously known | 10 cells: `9.0`→`9`, `10.0`→`10`, `12.0`→`12`, `17.0`→`17`, `21.0`→`21`, `25.0`→`25`, `29.0`→`29`, `30.0`→`30`, `31.0`→`31`, `34.0`→`34` | restored from the rebuild |
| 3 | **Both `#` comment lines reflowed** — not previously known | Numbers parsed them as data: the space after every comma was eaten (5 in line 1, 9 in line 2) and trailing empty fields were appended (`,,,,,,,` and `,,,`). The file also lost its final newline | comment lines restored verbatim; trailing newline restored |

**Nothing else was altered.** Verified cell by cell against the rebuild: 109 rows (unchanged),
`review_order` continuous 1–109 with no gaps or reordering, all 109 OBJECTIDs present and in the
same positions, and all 109 `assistant_reason` strings byte-identical including the `°`/`ß`
characters and the embedded commas and quotes. After repair, **zero** non-human cells differ from
the rebuild.

Two columns were added: `excluded_rule` and `excluded_reason` (§3). No rows were deleted. A third
`#` comment line documenting the repair and the exclusion rules was prepended.

## 2. Label counts after normalisation

`human_label` mapped `1`→`true`, `0`→`false`, `2`→`unclear`, empty→missing. **No value outside
`{0, 1, ''}` was present** — no `0.0`, no `1.0`, no `TRUE`, no `yes`, and no `2`. The mapping ran
in fail-loud mode and did not have to guess on any row.

| Normalised | Count |
|---|---|
| `true` | 6 |
| `false` | 97 |
| `unclear` | 0 |
| missing (not reviewed) | 6 |
| **total** | **109** |

- Queue was 88 rows; the human reviewer answered **85** of them. Three queue rows are blank: `213848`,
  `217211`, `217897` (review_order 39–41, consecutive — they look skipped rather than declined).
- The reviewer also answered **18 rows beyond the 88-row queue**, all `0`, out of the 21 non-queue rows.
- **6 rows are genuinely unreviewed**: `213848`, `216986`, `217211`, `217897`, `222220`, `225670`.
- The reviewer recorded **no `human_note` on any row**, and used `2` (cannot tell) **zero times** — every
  one of the 13 rows the assistant called `unclear` was resolved to `false`.

## 3. Exclusions

Applied blind to the detector, from the Phase 2 reasons and by opening each crop individually.
**9 rows excluded**, of the 8 proposed plus one more.

### E1 — no roof surface inside the outline (6)

| OBJECTID | Address | Justification |
|---|---|---|
| 355731 | 04., Treitlstraße 2 | 49 m² diamond over open lawn and a footpath in the Resselpark; no building beneath the outline. |
| 355737 | 04., Treitlstraße 2 | Second 49 m² diamond over the same parkland a few metres from 355731; no building beneath it. |
| 219006 | 02., Welthandelsplatz 1 | 24 m² sliver over asphalt paving and a kerb line in the WU service yard; no building beneath it. |
| 269353 | 02., Welthandelsplatz 1 | 20 m² quadrilateral over the concrete apron *beside* a small service shed, not over the shed roof. |
| 355040 | 04., Gußhausstraße 21 | 33 m² quadrilateral over a dark inner light well between four pitched roofs; no roof surface inside. |
| 372507 | 04., Gußhausstraße 18 | 26 m² diamond over a shadowed inner courtyard between four pitched roofs; no roof surface inside. |

### E2 — outline spans multiple separate structures (3)

| OBJECTID | Address | Justification |
|---|---|---|
| 202631 | 05., Spengergasse 7 | 4463 m² outline over an entire Gründerzeit block — a dozen separate structures plus two inner courtyards and gardens; "does this record have PV" has no single answer. |
| 258556 | 04., Gußhausstraße 25 | 8256 m² outline covering separate TU blocks on **both sides of Gußhausstraße** plus courtyards, a terrace and a driveway. |
| **264023** | 05., Spengergasse 27 | **Added.** 1883 m² outline enclosing a detached flat-roofed west block *and* the long pitched street wing, joined inside the outline only by open landscaped garden — lawn, paths, circular planters. Two structurally separate buildings under one record. |

264023 was found by re-reading all 109 Phase 2 reasons: its own reason already concluded "it
spans several structures", and zooming the crop confirms the two masses do not touch inside the
outline. It meets E2 on the same footing as 202631 and 258556.

### Deliberately **not** excluded

| OBJECTID | Why it was considered | Why it stays |
|---|---|---|
| 268882 (Gußhausstraße 4) | Phase 2 said "recommend excluding as a degenerate outline" — more than half the outline is a shadowed light well with a bare tree | E1 requires *no* roof surface inside. There is one: a readable grey metal pitched strip at the south end, on a single structure. The record has a single answer, so it is scoreable. |
| 211687 (Karlsplatz 13) | "the outline spans the whole quadrangle" | A quadrangle is one connected building; the courtyards are cut out by inner rings. Not E2. |
| 310979 (Spengergasse 38) | "part of the enclosed area is courtyard paving, not roof" | One continuous courtyard wing roof is inside the outline. Not E1. |
| 320598, 303044, 296100, and the shadow / sheeting rows | Blue construction sheeting over the whole outline; whole outline in deep shadow; specular flare over a third of it | **Deliberately kept in.** A detector that fires on blue sheeting, a shadow band or a sun star is producing a real defect on a real address and must be charged for it. Difficulty is not an exclusion criterion. |

All 9 excluded rows carry `human_label == false`, so the exclusions remove 9 easy negatives —
they make the false-positive test **harder**, not easier, which is the intent.

## 4. Control sample

15 rows drawn blind from the high-confidence `false` set with seed `20260806` and shuffled into
the queue. The human reviewer did not know which. All 15 were answered.

**Agreement: 15 / 15.** Zero disagreements.

Stated plainly: **this is not a zero error rate.** With 0 disagreements in 15 draws:

- rule-of-three upper bound: **3/15 = 20%**
- Clopper–Pearson 95% interval: **0.0% – 21.8%**

The assistant's error rate on high-confidence negatives is not distinguishable from zero at
n = 15 and could be as high as roughly one in five. That bound is far too loose to score against,
so **no assistant-only label enters any denominator** — every scoreable row carries a recorded human
label, and the 6 unreviewed rows are dropped rather than assumed clean.

For comparison, over the whole reviewed set:

| Set | n | Agreement |
|---|---|---|
| Blind control | 15 | 15 (100%) |
| All reviewed rows, strict (`unclear` counts as disagreement) | 103 | 86 (83.5%) |
| Reviewed rows where the assistant committed to `true`/`false` | 90 | 86 (95.6%) |

The gap between 83.5% and 95.6% is entirely the 13 `unclear` rows, which the human reviewer resolved
without exception to `false` — the assistant hedged where the reviewer did not.

## 5. The four disagreements

The human reviewer labelled `1` where the assistant had `false`. **The human label was retained at
this stage and was not overruled.** All four are recorded as `true` in `labels.csv`.

All four are array-versus-glazing mimic calls. Three were flagged in Phase 2 as explicitly hard
(`KNOWN MIMIC`, `HARDEST CALL IN THE SET`, `HARD CALL`). **None carries a `human_note`** — no
reviewer reasoning was recorded for any of them.

| OBJECTID / address | Assistant (`false`) | Human reviewer |
|---|---|---|
| **268929**, 05., Spengergasse 25 — *medium* | "KNOWN MIMIC. At 5x the fourteen blue-grey rectangles on the sedum roof are raised lantern rooflights: each has a bright white kerb frame, two internal glazing bars splitting it into three bays, and a shadow cast off its upstand; they sit ~5 m apart, not packed into rows." | `1` — array present. No note. |
| **248105**, 04., Gußhausstraße 23 — *low* | "HARDEST CALL IN THE SET. Three fields of dark rectangular panes on the courtyard-side roofs; at 6x each pane is bounded on all four sides by a continuous BRIGHT WHITE lattice and the fields sit perfectly flush and untilted, which reads as patent glazing — but flush-mounted modules cannot be ruled out at this resolution." | `1` — array present. No note. |
| **355083**, 04., Gußhausstraße 18 — *low* | "HARD CALL. A long band of narrow dark strips separated by pale bars; at 5x the bars run unbroken from eaves to ridge with no cross-breaks between rows, which reads as ribbed patent glazing over the mansard rather than a module field." | `1` — array present. No note. |
| **368679**, 04., Gußhausstraße 5 — *medium* | "Narrow 151 m² strip roof in the shade of the block, grey-blue with two small rooflights; a specular sun star flares across the street outside the outline but does not reach the roof." | `1` — array present. No note. |

### Limitation of the reference standard

This is a real weakness and is not smoothed over. The reference standard is a single reviewer,
unblinded to the assistant's proposals, and on the four rows where he overturned a reasoned
assistant call **no reasoning was recorded**. The assistant's reading on each of the four is
specific and falsifiable (white kerb frames, internal glazing bars, cast upstand shadows,
uninterrupted eaves-to-ridge bars). The reference label is a bare `1`. It stands by design, but
it cannot be audited, and 368679 is the one that most invites a second look: the assistant's
reason describes no module-like feature at all, only two small rooflights on a shaded strip roof.

If any of the four is wrong, it is wrong in the direction that matters least for the primary
metric — an over-called positive removes a roof from the negative denominator rather than adding
a spurious false positive — but it would inflate the already-tiny positive count.

### Consequence

**Positives go from 2 to 6.** That triples the positive count and it materially changes what the
sample supports: the positives are now dominated by mimic-adjacent calls (4 of 6), so any
detection on them is evidence about mimic discrimination rather than about clean-array recall.
It does not make recall measurable — 6 positives, 2 held out, is nowhere near enough — and
`preregistration.md` §3 forbids computing it at all.

## 6. Split

`splits.json`, seed **`20260807`**, fixed before any detector output was seen. Stratified by
`human_label`, 60% tune / 40% held-out.

Scoreable = `human_label` in `{true, false}` **and** not excluded: **94 rows** (109 − 6
unreviewed − 9 excluded). 6 positives, 88 negatives. No `unclear` rows exist to drop.

| Split | Total | Positives | Negatives |
|---|---|---|---|
| Tune | 57 | 4 — `268929`, `345054`, `355083`, `368679` | 53 |
| Held-out | 37 | 2 — `248105`, `298442` | 35 |

**What this makes measurable.** The 35 held-out reference negatives — 33 strict two-reader
agreements and 2 human-resolved assistant abstentions — support a false-positive
rate with a Clopper–Pearson interval. That is the whole measurement. Even a perfect 0/35 only
certifies FPR ≤ 10.0% at 95%.

**What it does not.** Two held-out positives support nothing. A recall figure from n = 2 can only
be 0%, 50% or 100%, and precision at this positive count is the FPR restated. Note also that both
held-out positives are of the contested kind — `298442` is the one unambiguous flush-mounted
array in the set, and `248105` is the row the assistant called the hardest in the set and got
overturned without a note. Whether the detector fires on `248105` is therefore not clean evidence
about anything.

## 7. Things that should change the pre-registration before it is fixed

1. **The label-error correction has almost nothing to correct.** The design assumed ~20 rows
   would go unreviewed and that the control would license using assistant labels on them with a
   widened interval. The human reviewer reviewed 18 of the 21 non-queue rows, so only 6 rows are unreviewed
   — and since the scoreable set is defined by recorded human labels, those 6 simply drop out. The control
   result therefore functions as a **justification for excluding assistant-only labels**, not as
   a correction factor applied to the primary metric. `preregistration.md` §5 is written that way.
   This disposition was carried into the frozen pre-registration.
2. **`2` was never used, and no note was ever written.** The human reviewer had "cannot tell" available and
   used it zero times, resolving all 13 assistant-`unclear` rows to `false`. Combined with the
   absence of `human_note` on all 109 rows, the reference standard carries no recorded
   uncertainty and no recorded reasoning anywhere. Every label is a bare, confident 0 or 1. If
   any of those 13 forced calls was in fact a coin flip, the negative denominator contains rows
   that are not reliable reference negatives, and the primary metric would charge the detector for
   firing on them.
3. **The threshold is a consequence of n, not only of product cost.** At n = 35 the best
   attainable certified bound is 10.0%, so "zero false positives" is not a conservative choice —
   it is the only choice that reaches the floor. This is stated in §2 of the pre-registration,
   but it should be a conscious decision: the alternative is to enlarge the negative sample
   before scoring rather than to pre-register a bar that a 35-row sample can barely express.
4. **Three consecutive queue rows are blank** (`213848`, `217211`, `217897`, review_order 39–41).
   That pattern suggests a scrolling slip rather than three deliberate non-answers. If they were
   skipped by accident, answering them adds three negatives — likely one to the held-out split,
   which is not nothing at n = 35.
5. **368679 deserves a second look before the reference is frozen** (§5). It is the only one of
   the four disagreements where the assistant's recorded reasoning describes nothing
   array-shaped at all.

---

*Historical checkpoint: nothing had been tuned and the detector had not been run. The pre-registration
was subsequently frozen before detector execution; final outcomes are reported in `docs/solar_evaluation.md`.*
