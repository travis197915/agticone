# 10 — Provider Selection non-selected branch shows Clean → Not Applicable

**Tracker item:** 33 (SOP) — claim `25XJ46879400`, Process 4 Steps 5 and 6

## The bug (as auditors reported it)

> Process 4 · Provider Steps 5 and 6 — the agent marked **clean**; it should
> reflect **Not Applicable**.

## Root cause (verified)

Provider Selection picks a provider record by the claim's **group model** via
mutually-exclusive branch steps:

```
Step 4 → 1A / AN / "No Group Model"
Step 5 → 2A / 2I
Step 6 → 3A
Step 7 → 3B
```

Exactly one branch applies (the one matching the claim's group model); the others
do not. But only the *selected* branch's sub-flow gets skipped via
`applicable_when` — a **non-selected** branch's HEADER row is left
`matched=False, skipped=False`. `trace_builder.node_audit_status` treats any
non-skipped row as "executed", so that dangling header rolls the whole step up to
**CLEAN** ("Agent marked clean"). On 25XJ46879400 (group model 3B) Steps 5 and 6
therefore wrongly showed clean instead of Not Applicable.

## The fix

`mark_provider_selection_branch_na_prod.py` — deterministic, no-LLM. A branch step
is "not selected" when its header row (`step:<psel>:<n>:0`) is `matched=False`,
not skipped, non-adverse, **and** the branch produced no defect. Every non-skipped
row of such a branch is flipped to `skipped=True` with a `NOT_APPLICABLE`
skip_reason so the step/agent rolls up to **NOT APPLICABLE**. The selected branch
(header `matched=True`) and any step with a real finding are left untouched.
Verdict recomputed but can never regress (only non-adverse, non-matched headers
are skipped). Kept in sync across `RuleEvaluation`, `ClaimTrace` (branch-header
entry → Skipped + N/A rationale), and `ClaimExecutiveSummary`.

## Run on prod

```bash
python prod_fixes/10_provider_selection_branch_na/mark_provider_selection_branch_na_prod.py --dry-run
python prod_fixes/10_provider_selection_branch_na/mark_provider_selection_branch_na_prod.py --apply
```

## Verify in the UI (25XJ46879400, group model 3B)

- **Process 4 · Provider Selection · Steps 5 and 6** (the non-selected 2A/2I and
  3A branches) → **Not Applicable** (greyed).
- **Step 7** (3B — the selected branch) still shows its real evaluation.
- Verdict unchanged.

> Related: use folder `09_provider_selection_group_model_or` first if the routing
> row itself is mis-evaluating (AND vs OR); this folder handles the presentation
> rollup of the branches that were correctly *not* selected.

## Execution-engine fix (status: NEEDS ENGINE CHANGE)

New runs still leave the non-selected branch header dangling as CLEAN. Two
equivalent engine options:

1. **Skip the non-selected branch header, not just its sub-flow.** When the
   group-model router selects a branch, the engine should mark the *other*
   branch **header** rows `skipped` (NOT_APPLICABLE) via the same
   `applicable_when` mechanism that already skips their sub-flows — in
   `n_execute_shapes.execute_shapes`. Then no dangling non-skipped header
   remains.
2. **Make `node_audit_status` disregard a lone non-matched, non-skipped,
   non-adverse header** when every other row of the step is skipped — treat that
   step as NOT_APPLICABLE rather than CLEAN. This lives in
   `execution_app/trace_builder.py` (`node_audit_status`).

Option 1 is preferred (it fixes the data, not just the rollup). Until it lands,
run this backfill after each batch that exercises Provider Selection.
