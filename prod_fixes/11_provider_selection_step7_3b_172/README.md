# 11 — Provider Selection Step 7 (3B) rule #172 marked not-matched

**Tracker item:** 23 (SOP · Bug) — claim `25XK02420100`, Process 4 Step 7 (3B-3B)

## The bug (as auditors reported it)

> Step 7 · 3B-3B · Rule #172 — the agent marked it **not matched**. This step is
> a **match** per the SOP. SOP Step 7 (3rd choice) should lead the agent to
> confirm the EOB *"FOF Roster requirement not met by Clinician"* reflecting on
> the claim.

## Root cause (verified)

Provider Selection Step 7 is a prioritized **"Nth choice" ladder**. The auditor's
UI **rule #172 = the 5th choice** (`step:12:7:5`), which should confirm the
provider-selection denial (EOB "FOF Roster requirement not met by Clinician") and
read **matched / CONFIRMED**. An earlier prod backfill
(`fix_provider_selection_confirm_denied_prod.py`) targeted the **3rd** choice
(`step:12:7:3`) by mistake, so #172 stayed not-matched. The claim's verdict was
already CLEAN; this is a per-step correction on the ladder.

## The fix

`apply_provsel_172_fix_prod.py` — fully self-contained, hardcoded to the 6
affected claims, deterministic, no-LLM. Per run it:

1. **Reverts** the wrong 3rd-choice change — `step:12:7:3` → original
   not-matched/DENY values.
2. **Applies** the correct 5th-choice fix — `step:12:7:5` → matched / CONFIRMED
   (#172, ALLOW).
3. Replaces the Provider Selection `ClaimTrace` entries with the corrected ones
   (all other SOP entries preserved); `final_status` + `explainability_json`
   recomputed (stays CLEAN).

The exact corrected eval + trace values are baked in as a compressed blob
captured from the verified prod-replica — nothing is read from disk or
re-derived. `RuleExecutionRun` verdict + `ClaimExecutiveSummary` are left
unchanged (already CLEAN). Transactional per claim: if a claim would not stay
CLEAN, the write rolls back.

Companions (context / earlier iterations, not the one to run):
- `fix_provider_selection_confirm_denied_prod.py` — the earlier (wrong-choice)
  confirm-denied fix this script reverts.
- `fix_provider_selection_oon_deny_prod.py` — related OON provider-selection
  denial confirmation.

## Run on prod

```bash
python prod_fixes/11_provider_selection_step7_3b_172/apply_provsel_172_fix_prod.py --dry-run
python prod_fixes/11_provider_selection_step7_3b_172/apply_provsel_172_fix_prod.py --apply
```

## Verify in the UI (25XK02420100)

- **Process 4 · Provider Selection · Step 7 (3B)**, rule #172 → **Matched /
  Confirmed**, rationale confirms the EOB "FOF Roster requirement not met by
  Clinician". The mistaken 3rd-choice row is back to its original state.
- Verdict stays **CLEAN**.

## Execution-engine fix (status: data/definition)

This is a per-claim ladder-position correction rather than an engine-logic bug —
the "Nth choice" ladder itself evaluates correctly (see the SEQUENTIAL / choice-
ladder handling in `n_execute_shapes.execute_shapes`, `_is_choice_ladder`). The
durable safeguard is to make sure the **UI rule number → `step:12:7:N` mapping**
is verified before any confirm-denied backfill targets a specific choice, so a
future fix cannot again patch the wrong ladder rung. If a fresh run still marks
#172 not-matched, the SOP row's condition for the 5th choice (the FOF-roster EOB
confirmation) needs the same CONFIRMED phrasing baked here promoted into the
`AuditDecision` / `NodeRuleBinding` definition.
