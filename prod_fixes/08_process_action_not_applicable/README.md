# 08 — Process-action steps (F3/F4 "Process the claim") → Not Applicable

**Tracker items:** 16, 19 (SOP · Improvement) — claims `25XK20940100`,
`25XK05953100`, `25XJ46879400`

## The bug (as auditors reported it)

> Across all SOPs: anytime there is a **process action** on a step — such as
> *"(F3) Process the claim"* — mark it **Not Applicable**. (Item 19 reopened
> because 25XK05953100 still reflected CLEAN, and Member-Eligibility step 14
> *"(F3) (File > Process)"* on 25XJ46879400 was the second example.)

## Root cause (verified)

Some SOP steps are not audit *checks* — they are a claim-**processing action** the
adjudicator keys once the audit is clean (Facets keystroke macros: `(F3) Process
the claim`, `(F3) (File > Process)`, `(F4) Save the claim`, `<Shift + F4> (File >
Save > Accept/Continue)`, etc.). The engine LLM-evaluated them and marked them
**Met** (a green ALLOW), so a "now process it" instruction looked like a passed
audit. Auditors want them shown as **Not Applicable** — neither pass nor defect,
simply outside the audit's remit.

## The fix

`mark_process_action_na_prod.py` — deterministic, no-LLM. A step qualifies when
its `decision_type` is **non-adverse** (never DENY/STOP/REFER/PEND/BYPASS — real
findings are left untouched) **and** its action text is a Facets keystroke macro:

- references an `F3`/`F4` keystroke (`\(?F[34]\)?`, incl. `<Shift + F4>`; **not**
  F5/F24/F51 which are pend/EOB codes), AND EITHER
- carries a process/save command (`process` | `save` | `accept` | `continue` |
  `File >`), OR
- is just the bare keystroke token (`(F3)`, `(F4)`, `<Shift + F4>`).

It deliberately does **not** match audit steps that merely mention "process"
without a keystroke (e.g. *"Process both the current claim and the claim in
history"*).

Representation (no schema change): the `RuleEvaluation` → `skipped=True,
matched=False` with a `skip_reason` that does **not** start with "out of scope"
(so `trace_builder.scope_category` → `NOT_APPLICABLE`, not `OUT_OF_SCOPE`); the
`ClaimTrace` step → `Skipped` (dashboard `normalizeAuditStatus` maps to
NOT_APPLICABLE) with the N/A rationale; the exec-summary line flipped in place.
Because each such step is its own shape/agent, the agent chip + group header roll
up to **NOT APPLICABLE** automatically. Only non-adverse rows are skipped, so the
verdict never regresses.

## Run on prod

```bash
# one claim first
python prod_fixes/08_process_action_not_applicable/mark_process_action_na_prod.py --claim 25XK05953100 --dry-run
python prod_fixes/08_process_action_not_applicable/mark_process_action_na_prod.py --claim 25XK05953100 --apply
# whole workflow
python prod_fixes/08_process_action_not_applicable/mark_process_action_na_prod.py --dry-run
python prod_fixes/08_process_action_not_applicable/mark_process_action_na_prod.py --apply
```

## Verify in the UI

- **Process 7 · Duplicate Verification · Step 9** *"(F3) Process the claim"* and
  **Process 2 · Member Eligibility · Step 14** *"(F3) (File > Process)"* show
  **Not Applicable** (greyed), not a green Met.
- The SOP/agent header for a step whose only content was a process action rolls
  up to **Not Applicable**, so 25XK05953100 no longer reads CLEAN on it.

## Execution-engine fix (status: NEEDS ENGINE CHANGE)

New runs still LLM-evaluate and mark these Met. Add a deterministic
**process-action detector** to the engine's Pass-1 skips so no LLM call is made
and the step is skipped as NOT_APPLICABLE:

- In `uhc-execution-engine/.../agents/n_execute_shapes.py`, inside the Pass-1
  loop (around line 660, alongside the `manual_oos` / `_rule_in_lob_scope` /
  `is_out_of_scope` branches), add an `is_process_action(rule)` check that
  matches the **exact same detector** this script uses (non-adverse
  `decision_type` + F3/F4/`<Shift+F4>` keystroke macro), and `mark_skipped(...)`
  with a `not-applicable:` reason.
- Keeping the detector identical to the script guarantees the forward-fix and the
  backfill agree row-for-row. (The script's module docstring references an
  `is_process_action` forward-fix; that function is **not yet present** in the
  engine — this is the change to make.)

Until it lands, run this backfill after each batch.
