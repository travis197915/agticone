# 03 — Timely Filing Step 5 "Adjustments/Appeals Submission" is out of scope

**Tracker item:** 37 (SOP) — claim `25XJ83640500`

## The bug (as auditors reported it)

> Process 6 · Timely Filing · Step 5 · Rule #22 — the agent marked **matched**
> for *IF: Adjustments/Appeals Submission AND • INN providers …*. **Adjustments/
> Appeals is Out of Scope.**

## Root cause (verified)

Step 5 lists several submission branches (COB, Resubmission/Corrected, New-day
claim, and **Adjustments/Appeals**). The Adjustments/Appeals branch
(`step:7:5:5`, RULE-005-006, decision_type `BYPASS`) was being evaluated and
marked matched. But adjustments/appeals have their own timely-filing windows and
are handled by a **separate** process — they are not audited by this claim's
timely-filing check. So that row is **out of scope**, not a match. The other
Step-5 branches remain in scope.

## The fix

`mark_timely_filing_adjustments_oos_prod.py` — deterministic, no-LLM, no re-run.

Note on *why* it uses `manual_oos_rule_keys` and **not**
`AuditDecision.is_out_of_scope`: the latter is a routing/**halt** flag ("when
Met, stop auditing this path") — the wrong tool. `Shape.properties`
`['manual_oos_rule_keys']` is the clean, non-halting exclusion the rule_loader
honours, so the row is skipped with no LLM and no routing side effect.

- **Phase A — definition (once):** add `step:7:5:5` to the Timely-Filing Step-5
  Shape's `properties['manual_oos_rule_keys']`; append a guidance clause to the
  Workbench `extra_context`. Future runs skip it as out of scope.
- **Phase B — backfill (per claim):**
  1. `RuleEvaluation` `step:7:5:5` → `skipped=True`, `out of scope:` skip_reason,
     `matched=False`, reasoning rewritten.
  2. `ClaimTrace` sub-rule RULE-005-006 → `Skipped` + out-of-scope statement;
     stale "matched/verified" rationale removed; step status / final_status /
     explainability recomputed.
  3. `ClaimExecutiveSummary` — a stale Step-5 "matched/verified" note softened in
     place (no regen, no LLM).

The row is `BYPASS` (non-adverse), so dropping it from the rollup is a
non-finding — the claim verdict is unchanged and a clean claim stays clean.

## Run on prod

```bash
python prod_fixes/03_timely_filing_adjustments_oos/mark_timely_filing_adjustments_oos_prod.py --dry-run
python prod_fixes/03_timely_filing_adjustments_oos/mark_timely_filing_adjustments_oos_prod.py --apply
```

## Verify in the UI

- **Process 6 · Timely Filing · Step 5** — the Adjustments/Appeals branch shows
  **Out of Scope** (greyed), not a green Met. COB / Resubmission / New-day
  branches are unchanged. Claim verdict unchanged.

## Execution-engine fix (status: shipped)

`rule_loader` honours `Shape.properties['manual_oos_rule_keys']` (see
`rule_loader.py` lines 273 & 430) and skips the listed rule with no LLM;
`n_execute_shapes` Pass-1 marks it skipped; `trace_builder.scope_category`
renders an `out of scope:` skip_reason as `OUT_OF_SCOPE`. Because Phase A writes
that property into the workflow, **future** runs already exclude the
Adjustments/Appeals branch — the backfill only repairs runs executed before it
was set.
