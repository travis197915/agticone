# 13 — LOB gating: Medicare-only SOPs must not run on non-Medicare claims

**Tracker context:** the LOB thread — *"Seems we are not using the LOB variable.
The Medicare opt-out and NPI check should not be called for non-Medicare."*
(Related to the auditor Medicare-tool complaint that also drove folder 06.)

## The bug (as auditors reported it)

> We should use the **LOB** variable to decide, per step, what runs. The
> **Medicare opt-out** and **NPI check** flows should **not** be called for
> non-Medicare (Commercial / Medicaid) claims. In the UI a non-Medicare claim
> should show those steps skipped with *"LOB is non-Medicare, hence it is not
> going through"*.

## Root cause (verified)

Two SOPs are Medicare-specific per the SOP flowchart ("Is the Plan Medicare? →
Yes"):

- **"Provider Name and NPI Validation Audit Guidelines"** (the NPPES/NPI check)
- **"Provider Opt-Out Look-Up Audit Guidelines"** (Medicare opt-out)

They carried **no LOB scope**, so they ran for **every** claim. LOB is already
derived per claim (`determine_claim_lob` → `RuleExecutionRun.claim_lob`); this fix
simply puts it to work at the SOP level. (Folder 06 handles the finer-grained
*per-tool* case where a single tool inside an all-LOB SOP is Medicare-only.)

## The fix

`scope_medicare_steps_prod.py` — deterministic, no-LLM.

- **Phase A — workflow scoping (forward, once):** set
  `Workbench.config['lob_scope'] = ['Medicare']` on the two SOP columns. The
  engine already honours this (`_workbench_lob_scope` → `_rule_in_lob_scope`), so
  every future non-Medicare claim skips those SOPs' rules with no LLM and the UI
  greys them.
- **Phase B — backfill (non-Medicare runs):**
  1. `RuleEvaluation` (`step:<sop>:*`) → `skipped=True, matched=False` with an LOB
     skip_reason (excluded from verdict, greyed in UI).
  2. `RuleExecutionRun` verdict recomputed from surviving evals.
  3. `ClaimTrace` — the two SOPs' entries → `Skipped` (tools → `tools_skipped`);
     `final_status` + `explainability_json` recomputed.
  4. `ClaimExecutiveSummary` updated **in place** (no regen, no LLM): verdict set,
     and the two Medicare-only `step_summaries` lines flipped to `OUT_OF_SCOPE`
     with the LOB message. (The old full regen did ~233 SQL round trips/claim —
     minutes each over the prod link; in-place is ~1 query.)

Medicare claims are untouched (those SOPs legitimately run). Idempotent.

`list_medicare_test_claims.py` (copied here) lists Medicare vs non-Medicare
claims so you can pick one of each to eyeball both UI states.

## Run on prod

```bash
python prod_fixes/13_lob_gating_medicare_steps/list_medicare_test_claims.py        # pick test claims
python prod_fixes/13_lob_gating_medicare_steps/scope_medicare_steps_prod.py --dry-run
python prod_fixes/13_lob_gating_medicare_steps/scope_medicare_steps_prod.py --apply
```

## Verify in the UI

- **Non-Medicare claim:** the NPI-validation and Provider-Opt-Out SOP columns are
  **greyed / Out of Scope**; the collapsed process header shows the rationale
  *"LOB is non-Medicare, hence it is not going through"*. Their tools appear under
  **skipped**. Verdict unchanged.
- **Medicare claim:** both SOPs run and evaluate normally.

## Execution-engine fix (status: shipped)

LOB gating is live end-to-end:

- `n02_load_bindings.py` calls `determine_claim_lob` and sets `claim_lob` +
  `lob_out_of_scope` on the state (lines 62/128).
- `n_execute_shapes.py` — `_rule_in_lob_scope(rule)` (line 269) skips a rule whose
  `lob_scope` excludes the claim's LOB during Pass-1 (no LLM, line ~676); the
  whole-claim `lob_out_of_scope` gate is at line 510.
- `Workbench.config['lob_scope']` is hydrated onto each rule by `rule_loader`
  (`_workbench_lob_scope`), which is what Phase A sets.
- `trace_builder.scope_category` renders the LOB skip as `OUT_OF_SCOPE`; the
  frontend surfaces the LOB rationale in the collapsed process header.

So a **new** non-Medicare run already skips both Medicare-only SOPs — the backfill
only repairs runs executed before Phase A was applied.
