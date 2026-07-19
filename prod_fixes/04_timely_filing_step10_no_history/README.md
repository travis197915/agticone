# 04 — Timely Filing Step 10 false defect (no claim in history)

**Tracker item:** 38 (SOP) — claim `25XJ83640500`

## The bug (as auditors reported it)

> Process 6 · Timely Filing · Step 10 **reflects a defect**. The claim has been
> adjusted; our audit is off. The agent **correctly** determined the claim was
> denied for timely filing and it should be **clean**. **There is no claim in
> history.**

## Root cause (verified against the persisted prod rows)

On a NEW-DAY claim correctly denied for timely filing with **no** matching claim
in history, two Step-10 branches — neither of which is an audit finding — flip
the stored run to DENY / DEFECT:

- **(A) TRACE side.** `step:7:10:2` — *"No claim/line in history, Freq 7/8
  Submission" → "Follow OBH Facets Kill-Delete Reroute Process"*. The branch
  simply does not apply, so the trace entry renders **Not-Met (CONDITIONAL)**.
  But `trace_builder.claim_status` rolls **any** `Not-Met` up to DEFECT (it is
  status-string driven, not disposition driven), so a routing branch that merely
  "did not apply" surfaces as the "Step 10 No Claim in History Chart" DEFECT.
- **(B) EVAL side.** `step:7:10:3` — *"New Day claim/line denying for timely
  filing → allow the system to deny for timely filing"*, `decision_type=DENY`.
  This row **confirms** the timely-filing denial is correct (a valid disposition,
  not a processor error), but its `DENY` type makes the run aggregator set
  `RuleExecutionRun.final_decision_type = DENY`.

Together, a correctly-adjudicated history-free timely-filing denial renders as
**DEFECT (DENY)** instead of **CLEAN (ALLOW)**.

Prod confirmation (claim 25XJ83640500): `final=DENY`, trace `final_status=DEFECT`,
`step:7:10:3 matched=True dt=DENY`, and a `Not-Met` Step-10 "Kill-Delete Reroute"
trace entry.

## The fix

`fix_tf_step10_no_history_defect_prod.py` — deterministic, no-LLM, idempotent.
For each targeted run whose **only** defect is this Step-10 false positive:

1. Flip the `step:7:10:3` "allow the system to deny" eval(s) → skipped /
   Not-Applicable (removes the eval-side DENY).
2. Flip the Not-Met CONDITIONAL Step-10 "No claim in history / Kill-Delete
   Reroute" trace entrie(s) → Skipped / Not-Applicable (removes the trace-side
   DEFECT).
3. Recompute `RuleExecutionRun.final_decision_type`, `ClaimTrace.final_status` +
   `explainability_json` (via the real `trace_builder`), and flip the exec-summary
   Step-10 line + verdict/headline.

**Safety guard:** the transaction commits only if, after patching, the run
recomputes to `final_decision_type == ALLOW` **and** `final_status == CLEAN`. If
the claim has any other real finding it is rolled back and left untouched — this
script can only turn a claim whose *sole* defect is this Step-10 false positive
from DEFECT → CLEAN, never the reverse.

## Run on prod

```bash
# 1) verify the flagged claim first (writes nothing)
python prod_fixes/04_timely_filing_step10_no_history/fix_tf_step10_no_history_defect_prod.py --claim 25XJ83640500 --dry-run
# 2) fix just that claim
python prod_fixes/04_timely_filing_step10_no_history/fix_tf_step10_no_history_defect_prod.py --claim 25XJ83640500 --apply
# 3) whole workflow (all same-signature claims)
python prod_fixes/04_timely_filing_step10_no_history/fix_tf_step10_no_history_defect_prod.py --dry-run
python prod_fixes/04_timely_filing_step10_no_history/fix_tf_step10_no_history_defect_prod.py --apply
```

Dry-run on 25XJ83640500 prints:
`[CHANGED] 1 eval + 1 trace step-10 row(s) -> N/A; DENY->ALLOW / DEFECT->CLEAN`.

## Verify in the UI

- **Process 6 · Timely Filing · Step 10** — the "No claim in history / Kill-Delete
  Reroute" branch shows **Not Applicable** (not a red defect).
- Claim rolls up **CLEAN / ALLOW**; exec-summary no longer flags Step 10.

## Execution-engine fix (status: NEEDS ENGINE CHANGE)

The backfill repairs existing runs, but **new** runs still reproduce it. Two
engine changes make it correct at the source:

1. **Trace rollup should be disposition-driven, not status-string-driven.** In
   `execution_app/trace_builder.py`, `claim_status` currently promotes any
   `Not-Met` trace entry to DEFECT. A **routing** branch that "did not apply"
   (CONDITIONAL, no adverse code, not a real audit check) should not count as a
   defect. Gate the Not-Met → DEFECT promotion on the entry actually being an
   audit finding (has an adverse `decision_type`/EOB code), not merely Not-Met.
2. **A "confirm the system denial is correct" row must not be adverse.** The
   `step:7:10:3` "allow the system to deny for timely filing" row is a system
   confirmation, so model it as `decision_type=SYSTEM` (like the CDD confirm in
   folder 01) instead of `DENY`, so `n06_aggregate` does not set the run to DENY.

Until both land, keep running this backfill after each batch that contains
history-free timely-filing denials.
