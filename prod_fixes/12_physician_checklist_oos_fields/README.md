# 12 — Physician Claim Checklist: institutional/adjudication fields out of scope

**Tracker items:** 18, 25, 26, 27, 28, 29, 30, 31 (SOP · Bug/Improvement) —
claim `25XJ88210600`, Process 1 · Initial Verification · Step 1

## The bug (as auditors reported it)

The Physician Claim Checklist (professional / CMS-1500 claim) marked a set of
mandatory fields as **matched / "Verified per SOP"** even though they are **not
present** on the Doc360 image or the Facets indicative screen. Auditors flagged
each as **out of scope**, not a clean match:

| Field | Rule # | Why out of scope |
|---|---|---|
| COB (Coordination of Benefits) | #227 | out of scope |
| Authorization | #229 | no auth data on image |
| Statement Covers Period (To & From) | #229 | UB-04 institutional field |
| Type of Bill | #232 | UB-04 institutional field |
| Accommodation Rates | #233 | UB-04 institutional field |
| Copay | #234 | adjudication output |
| Coinsurance | #235 | adjudication output |
| Deductible | #236 | adjudication output |
| Type of Service / Paid Amount | (PDF) | highlighted out of scope |

Item 18 is the related SOP-link/wording clarification: the checklist is mandatory
on **every physician claim** (not just adjustments), and should list all fields.

## Root cause (verified)

The workflow modelled these fields but had no way to say "present in the SOP but
out of scope for a physician image". So the agent evaluated them and, lacking
contradicting evidence, marked them matched — a false clean. `Provider ID (TIN)`
(row 11) was **not** flagged and stays in scope.

## The fix (two scripts)

### `add_physician_checklist_fields_prod.py` — complete the mandatory set
Item 18: the checklist only modelled 10 of the SOP's mandatory fields. Phase A
adds the remaining 11 `AuditDecision` sub-rules (RULE-001-011..021, row 10..20,
CONDITIONAL) under SOP 14 Step 1 and the matching `NodeRuleBinding` rows, so the
engine evaluates the full checklist on every future run. Phase B backfills
existing claims (all confirmed CLEAN by auditors) as verified.

### `mark_physician_checklist_oos_prod.py` — scope the non-image fields OOS
Marks the 10 institutional/adjudication rows (`step:14:1:{10,12..20}`) out of
scope. Uses `Shape.properties['manual_oos_rule_keys']` — the clean, non-halting
exclusion the rule_loader honours — **not** `AuditDecision.is_out_of_scope`
(which is a routing/halt flag).

- **Phase A — definition (once):** add the 10 rule_keys to the Step-1 Shape's
  `manual_oos_rule_keys`; append a guidance clause to the Workbench
  `extra_context`. Future runs skip them as out of scope, no LLM, no halt.
- **Phase B — backfill:** `RuleEvaluation` → skipped/`out of scope:`;
  `ClaimTrace` sub-rules → Skipped + out-of-scope statement (stale "verified"
  segments removed); `ClaimExecutiveSummary` note softened in place. These rows
  are CONDITIONAL (non-adverse), so verdict is unchanged — a clean claim stays
  clean.

## Run on prod (add the full set first, then scope the non-image ones OOS)

```bash
python prod_fixes/12_physician_checklist_oos_fields/add_physician_checklist_fields_prod.py --dry-run
python prod_fixes/12_physician_checklist_oos_fields/add_physician_checklist_fields_prod.py --apply
python prod_fixes/12_physician_checklist_oos_fields/mark_physician_checklist_oos_prod.py --dry-run
python prod_fixes/12_physician_checklist_oos_fields/mark_physician_checklist_oos_prod.py --apply
```

## Verify in the UI (25XJ88210600)

- **Process 1 · Initial Verification · Step 1** — COB, Authorization, Statement
  Covers Period, Type of Bill, Accommodation Rates, Copay, Coinsurance,
  Deductible show **Out of Scope** (greyed), not a green Met. Subscriber ID,
  Member Name, Provider ID (TIN), etc. still show their real evaluation.
- Verdict unchanged.

## Execution-engine fix (status: shipped)

`rule_loader` honours `Shape.properties['manual_oos_rule_keys']`
(`rule_loader.py` lines 273 & 430); `n_execute_shapes` Pass-1 skips those rows
with no LLM; `trace_builder.scope_category` renders an `out of scope:` skip_reason
as `OUT_OF_SCOPE`. Because Phase A writes the property (and the new field set is
in the definition), **future** runs already present the checklist correctly — the
backfill only repairs earlier runs.

Ingestion follow-up (item 18): the SOP source (KL) mis-framed the mandatory list
as "for adjustments/resubmissions". The workflow already runs the checklist on
every claim with no adjustment/LOB gate, so the presentation is correct; if the
SOP is re-ingested, re-run `add_physician_checklist_fields_prod.py` to restore
the complete field set.
