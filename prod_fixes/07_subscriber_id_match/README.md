# 07 — Subscriber ID marked not-matched when it is the same identity

**Tracker items:** 14, 24, 39 (SOP · Bug) — claims `25XJ46879400`,
`25XK05953100`, `25XJ57727400` (Process 1, rules #202/#203)

## The bug (as auditors reported it)

- **Item 14:** Subscriber ID image `OSC7640641001` and Facets *Standard Unique
  Health ID* `OSC7640641001` match, but the agent reflects **not matched**.
  *"We have 3 unique fields in Facets to match a subscriber ID — confirm all
  locations are reviewed."* Rule #203 should be **matched**.
- **Item 39:** Subscriber ID `N32669686` is a match in Facets → *Transfer
  Subscriber Family > Subscriber > Additional ID*; agent did not match.
- **Item 24:** Subscriber ID mismatch (facets vs image) should be an **Error**,
  not silently marked N/A.

## Root cause (verified)

The Subscriber-ID sub-rule (SOP 14, RULE-001-001, `step:14:1:0`) compared the
Facets Subscriber ID to the Doc360 image *"Insured's ID Number"* with a plain
equality check. The image carries the **member ID** = Facets **subscriber base
ID (SBSB_ID) + 2-digit member suffix**, sometimes with a plan/product alpha
prefix (`OS`, `STAS`, `M2K`) or leading-zero normalization. Equality therefore
failed on genuine matches:

```
SBSB_ID K61297021  == Doc360 K6129702101    (base + member suffix 01)
SBSB_ID 999095985  == Doc360 M2K999095985   (plan prefix + base)
SBSB_ID C77772744  == Doc360 OSC7777274401  (prefix + base + suffix)
```

A prior backfill compounded it by marking any non-equal pair "masked / Not
Applicable". And some matches (item 39: `N32669686`) live only on the Facets
**Additional-ID** screen, which our Facets tools do not return — provable only by
auditor attestation.

## The fix

`fix_subscriber_id_match_prod.py` — deterministic, grounded in stored data,
no-LLM. Per claim's latest run it reads the SBSB_ID from the stored
`facets_get_summary` result and the image ID from the ClaimTrace sub-rule, then
flips to **Met** only when the match is **provable**:

- **EXACT** — normalized image == normalized SBSB_ID.
- **PROVABLE** — normalized SBSB_ID is a contiguous substring of the image ID
  (base + suffix and/or prefix), or the digit cores contain one another (≥6
  digits).
- **ATTESTED** — claim id passed via `--attested` (auditor verified the
  Additional-ID match by hand; default list includes `25XJ57727400`).

Everything else is **left untouched** — never asserts a match it cannot prove.
Writes `RuleEvaluation` `step:14:1:0` → matched/CONDITIONAL, the `ClaimTrace`
sub-rule → Met, and scrubs stale masked/mismatch phrasing in
`ClaimExecutiveSummary` (in place). The rule is CONDITIONAL (non-adverse) so the
verdict is unaffected.

Companions:
- `fix_subscriber_id_false_negatives_prod.py` — the broader false-negative sweep.
- `mark_subscriber_id_masked_na_prod.py` — the prior "masked → N/A" behaviour,
  kept for reference/rollback context.

## Run on prod

```bash
python prod_fixes/07_subscriber_id_match/fix_subscriber_id_match_prod.py --dry-run
python prod_fixes/07_subscriber_id_match/fix_subscriber_id_match_prod.py --apply
# add auditor-attested Additional-ID claims (repeatable):
python prod_fixes/07_subscriber_id_match/fix_subscriber_id_match_prod.py --attested 25XJ57727400 --apply
```

## Verify in the UI

- **Process 1 · Initial Verification · Step 1**, Subscriber ID rule (#202/#203) —
  shows **Met** with a concrete match statement (e.g. *"image OSC7640641001
  matches Facets SBSB_ID C77772744 + member suffix"*). Verdict unchanged.

## Execution-engine fix (status: partial)

- **Identity normalization is the durable fix.** The Subscriber-ID check should
  compare on a normalized identity (strip plan/product prefix, drop the member
  suffix, digit-core containment) rather than raw equality. That normalization
  lives in the SOP-14 subscriber-ID evaluation / tool-context (`_eval_common`
  builds what the check sees); porting the `fix_subscriber_id_match_prod.py`
  matcher into it makes new runs match without a backfill.
- **Additional-ID field is a data-source gap.** `N32669686` lives on Facets
  *Transfer Subscriber Family > Additional ID*, which the current `facets_*`
  tools do not return. Closing this fully needs that field added to the summary
  tool; until then those specific claims require auditor attestation
  (`--attested`).
- **Mismatch = Error (item 24).** A genuine, unprovable mismatch should surface as
  an adverse/Error disposition, not be silently N/A'd — the script deliberately
  leaves unprovable pairs untouched so a real mismatch still reads as a finding.
