# Final human-review checklist

Model-assisted QA covered an earlier baseline; **this checklist is the human review of the current artifacts**. Work through it, then record the review — the strict release gate (`propx-roofs release-check --require-current-audit`) stays at exit code 3 until a completed review of exactly these bytes is recorded.

- Document: `outputs/roof_attributes.json`
- Document sha256: `6293c3e70dfb34e41e67cae297e202943f845fc096bfc915fd0d763534b7dba1`
- Generated at (cited from the document): `2026-08-08T17:14:19.566891+02:00`

## How to review

1. Open each building's overlay PNG (paths in the table).
2. Check the published values in that building's row against what the imagery shows, and read its review-flag triggers (full audit notes: `validation/audit_annotations.json`).
3. Tick the building's checkbox. Do not tick what you did not review.
4. When every box is ticked, record the review (this asserts the review happened):

   ```
   propx-roofs record-review --reviewer "Mohammad"
   ```

## Buildings

| building_id | address | roof_type | area m2 | ridge | surface (dominance) | green_roof | review-flag triggers | overlay |
|---|---|---|---|---|---|---|---|---|
| vie-swv-001 | 10., Karl-Popper-Straße 4 | flat | 2319.5 | withheld | gravel_or_bitumen_like (73%) | not_detected | - | `overlays/vie-swv-001.png` |
| vie-swv-002 | 10., Wiedner Gürtel 5 | flat | 6281.7 | withheld | mixed (54%) | not_detected | visual_audit_questionable | `overlays/vie-swv-002.png` |
| vie-swv-003 | 10., Gertrude-Fröhlich-Sandner-Straße 3 | pitched | 1092.04 | withheld | gravel_or_bitumen_like (72%) | not_detected | - | `overlays/vie-swv-003.png` |
| vie-swv-004 | 10., Sonnwendgasse 3 | pitched | 3364.35 | withheld | gravel_or_bitumen_like (75%) | not_detected | visual_audit_questionable | `overlays/vie-swv-004.png` |
| vie-swv-005 | 04., Wiedner Gürtel 14 | flat | 548.24 | withheld | gravel_or_bitumen_like (59%) | not_detected | visual_audit_questionable | `overlays/vie-swv-005.png` |
| vie-swv-006 | 10., Gerhard-Bronner-Straße 7 | flat | 597.9 | withheld | gravel_or_bitumen_like (85%) | not_detected | visual_audit_questionable | `overlays/vie-swv-006.png` |
| vie-swv-007 | 04., Wiedner Gürtel 26 | pitched | 601.86 | 65.04 deg | mixed (41%) | not_detected | visual_audit_questionable, weak_cv_agreement | `overlays/vie-swv-007.png` |
| vie-swv-008 | 04., Wiedner Gürtel 16 | pitched | 682.4 | withheld | gravel_or_bitumen_like (83%) | not_detected | visual_audit_questionable | `overlays/vie-swv-008.png` |
| vie-swv-009 | 04., Argentinierstraße 69 | pitched | 454.85 | withheld | gravel_or_bitumen_like (85%) | not_detected | visual_audit_questionable, weak_cv_agreement | `overlays/vie-swv-009.png` |
| vie-swv-010 | 04., Wiedner Gürtel 24 | pitched | 384.97 | withheld | gravel_or_bitumen_like (72%) | withheld | low_judgeability, visual_audit_questionable, withheld_attributes | `overlays/vie-swv-010.png` |

## Sign-off

- [ ] **vie-swv-001** — opened `overlays/vie-swv-001.png`, checked every published value in the row above against the imagery, and read this building's review-flag triggers
- [ ] **vie-swv-002** — opened `overlays/vie-swv-002.png`, checked every published value in the row above against the imagery, and read this building's review-flag triggers
- [ ] **vie-swv-003** — opened `overlays/vie-swv-003.png`, checked every published value in the row above against the imagery, and read this building's review-flag triggers
- [ ] **vie-swv-004** — opened `overlays/vie-swv-004.png`, checked every published value in the row above against the imagery, and read this building's review-flag triggers
- [ ] **vie-swv-005** — opened `overlays/vie-swv-005.png`, checked every published value in the row above against the imagery, and read this building's review-flag triggers
- [ ] **vie-swv-006** — opened `overlays/vie-swv-006.png`, checked every published value in the row above against the imagery, and read this building's review-flag triggers
- [ ] **vie-swv-007** — opened `overlays/vie-swv-007.png`, checked every published value in the row above against the imagery, and read this building's review-flag triggers
- [ ] **vie-swv-008** — opened `overlays/vie-swv-008.png`, checked every published value in the row above against the imagery, and read this building's review-flag triggers
- [ ] **vie-swv-009** — opened `overlays/vie-swv-009.png`, checked every published value in the row above against the imagery, and read this building's review-flag triggers
- [ ] **vie-swv-010** — opened `overlays/vie-swv-010.png`, checked every published value in the row above against the imagery, and read this building's review-flag triggers

Withheld means the pipeline abstained; confirm the abstention is plausible rather than hunting for a value. Review can describe; it can never edit — nothing in this checklist changes any published value.
