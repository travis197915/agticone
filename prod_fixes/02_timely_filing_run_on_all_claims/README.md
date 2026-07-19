# 02 — Timely Filing must run on ALL claims (drop the TF0/TF1 gate)

**Tracker item:** 9 (SOP · Bug) — claim `25XK20940100`, "executive summary bullet 4"

## The bug (as auditors reported it)

> The agent should confirm timely filing on **all** claims — not only when the
> claim has a TF0/TF1 denial. For Step 1, ignore the *"Is your claim/line denying
> for TF1 or TF0?"* check. It should run on all claims. **Change the status to
> Not Applicable and ignore the step.**

## Root cause (verified)

The Timely Filing SOP (`OBH_Facets_Timely_Filing`, sop_id 7) modelled **Step 1**
as a TF0/TF1-denial gate. On claims without a TF0/TF1 denial the gate did not
match and (depending on routing) short-circuited or muddied the downstream steps,
so timely filing was not being confirmed on every claim. Auditors want Step 1
itself to simply read **Not Applicable** and disappear, while every other Timely
Filing step continues to run and be evaluated normally.

## The fix

`scope_timely_filing_na_prod.py` — deterministic, no-LLM, in place. Scope is
**only** the Step 1 rows (`step:7:1:*`); every other TF row (`step:7:2:*` … and
the `pre:7:*` preconditions) is untouched. Per claim's latest terminal run:

1. `RuleEvaluation` each `step:7:1:*` → `skipped=True, matched=False` with a
   `not-applicable:` skip_reason (**not** "out of scope", so
   `trace_builder.scope_category` → `NOT_APPLICABLE`) and blank `reasoning`.
2. `RuleExecutionRun` verdict recomputed from surviving evals (Step 1 is a
   non-scoring routing gate, so the verdict normally does not move; recompute is
   defensive and clears any TF-only halt that stuck).
3. `ClaimTrace` Step 1 entry (`sop_step_number == 1`) → `Skipped`, blank
   rationale/sub-rules, tools moved to `tools_skipped`; `final_status` +
   `explainability_json` recomputed.
4. `ClaimExecutiveSummary` verdict/status synced in place if it changed (no
   regen, no LLM). TF step summaries stay intact — the SOP still runs.

`fix_timely_filing_step1_scope.py` is the workflow-definition companion (sets the
Step 1 scope so future runs skip it); `rerun_timely_filing_prod.py` optionally
re-executes TF for a claim list against the fixed workflow.

## Run on prod

```bash
python prod_fixes/02_timely_filing_run_on_all_claims/scope_timely_filing_na_prod.py --dry-run
python prod_fixes/02_timely_filing_run_on_all_claims/scope_timely_filing_na_prod.py --apply
```

## Verify in the UI

- **Process 6 · Timely Filing · Step 1** ("Is your claim/line denying for TF1 or
  TF0?") shows **Not Applicable** with nothing in its rationale.
- All other Timely Filing steps still show their real Met/Not-Met evaluation, so
  timely filing is confirmed on the claim regardless of a TF0/TF1 denial.

## Execution-engine fix (status: shipped)

Step 1 is now scoped out at the definition level. The engine's Pass-1
deterministic skips (`n_execute_shapes.execute_shapes`, the `manual_oos` /
`is_out_of_scope` branches around line 660) already skip a scoped/OOS row with
**no LLM call**, and `trace_builder.scope_category` (line 202) classifies a
`not-applicable:` skip_reason as `NOT_APPLICABLE`. The remaining TF steps run on
every claim because they no longer sit behind the TF0/TF1 gate.
