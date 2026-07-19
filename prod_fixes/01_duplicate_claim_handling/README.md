# 01 — OBH Facets Duplicate Claim Handling

**Tracker items:** 1, 4, 13 (SOP · Bug) — claim `25XK20940100`

## The bug (as auditors reported it)

1. **Item 1 — Step 2 criteria missing.** Duplicate Verification Step 2 must
   compare the claim against a potential duplicate on **Member, Date of Service
   (DOS), Procedure (excluding modifiers)** and Provider NPI/TIN. Step 2 was
   collapsed to a single, non-evaluable row with no routing, so those criteria
   were never checked.
2. **Items 4 / 13 — Step 7 CDD reasoning.** The claim is denying as a duplicate
   via the **CDD** edit, but the agent's reasoning read *"there is no evidence of
   a CDD edit"* and never agreed the claim was correctly denied for CDD. The
   auditor wants the claim to read **CLEAN** — the audit agrees the system
   correctly denied the duplicate.

## Root cause (verified)

- **Step 2:** For the duplicate SOP variants actually bound into the workflow
  (6, 8, 9), ingestion collapsed Step 2 to one row (`condition "(applies to this
  step)"`, action `"Claim Criteria: Provider NPI or TIN"`) with **no routing**.
  The engine never evaluated Member/DOS/Procedure and never applied the
  All-match → Step 4 / Any-mismatch → Step 3 routing. The duplicate-claim tool
  was bound to a single row, so the other routing row reasoned blind.
- **Step 7 / CDD:** The confirm row (Step 7/8 row 0 — *"allow the system to deny
  the duplicate and proceed to Step 9"*) was modelled as an adverse **`DENY`**
  with EOB codes **E51/F51**. So a claim the system correctly denied as a
  duplicate surfaced as **DEFECT/DENY**, and the reasoning contradicted the CDD
  denial. Per the SOP this confirm is a **CLEAN** audit — a system disposition
  the audit *agrees* with, not an auditor-raised denial.

## The fix

Definition correction + a no-LLM per-claim backfill (≈200 claims), in one shot.

- `fix_duplicate_step2_rules.py` — rewrites Step 2 to a single criteria
  determination with two complementary routing rows (All-match → Step 4 /
  Any-mismatch → Step 3), each enumerating all four criteria, and detaches
  `facets_get_duplicate_claim` to **shape scope** so both routing rows see the
  same duplicate-candidate evidence.
- `fix_duplicate_step7_line_details.py` — binds `facets_get_line_details`
  shape-scoped to Step 7/8, reclassifies the confirm row **`DENY` → `SYSTEM`**
  (clears E51/F51), and swaps in the affirmation clause.
- `seed_duplicate_cdd_clean_prod.py` — **the one you run.** Phase A invokes the
  Step-7 workflow correction (as a subprocess — that is why
  `fix_duplicate_step7_line_details.py` is copied alongside it). Phase B, for
  each CDD claim's latest run and with **no LLM**:
  1. `RuleEvaluation` dup confirm row → `decision_type='SYSTEM'`, `codes=['CDD']`,
     reasoning rewritten to *"system correctly denied as a duplicate (CDD); audit
     agrees"*.
  2. `RuleExecutionRun` verdict recomputed like the dashboard rollup → `ALLOW`
     when the dup confirm was the only adverse row.
  3. `ClaimTrace` Step 7/8 entry flipped to `Met`/`SYSTEM`/`['CDD']`;
     `final_status` + `explainability_json` recomputed.
  4. `ClaimExecutiveSummary` regenerated via the deterministic **no-LLM fallback**
     so the summary tab matches (headline: *"Claim correctly denied as a
     duplicate; system processing approved."*).
- `rerun_duplicate_verification_prod.py` — optional: re-executes duplicate
  verification through the (now-fixed) workflow for a claim list, if you prefer a
  real re-run over the backfill.

## Run on prod

```bash
# preview everything (workflow fix + per-claim seed)
python prod_fixes/01_duplicate_claim_handling/seed_duplicate_cdd_clean_prod.py --dry-run
# commit
python prod_fixes/01_duplicate_claim_handling/seed_duplicate_cdd_clean_prod.py --apply
# a single claim / an excel list of claim ids
python prod_fixes/01_duplicate_claim_handling/seed_duplicate_cdd_clean_prod.py --apply --claim 25XK20940100
python prod_fixes/01_duplicate_claim_handling/seed_duplicate_cdd_clean_prod.py --apply --claims-file claims.csv
```

If Step 2 routing was also stale on your workflow, run the Step-2 fix once first:

```bash
python prod_fixes/01_duplicate_claim_handling/fix_duplicate_step2_rules.py --apply
```

## Verify in the UI (claim 25XK20940100)

- **Process 7 · Duplicate Verification · Step 7/8** shows a **Met / System**
  chip (not DENY), rationale reads *"system correctly denied as a duplicate
  (CDD); audit agrees"* — no "no evidence of a CDD edit".
- Claim rolls up **CLEAN / ALLOW**.
- **Overall Claim Process Summarization** headline: *"Claim correctly denied as a
  duplicate; system processing approved."*

## Execution-engine fix (status: shipped)

The engine already treats `decision_type=SYSTEM` as a **non-adverse** disposition
in the aggregate rollup (`n06_aggregate.py`) and in `trace_builder`'s
`_DEFECT_DECISIONS` set, so a system-confirmed duplicate no longer counts as a
finding. The durable definition changes live in the workflow (`NodeRuleBinding` /
`AuditDecision` for the dup SOPs): confirm row is `SYSTEM` with `['CDD']`, Step 2
has two routing rows, and the duplicate tool is shape-scoped. Any **new** run of a
CDD duplicate therefore comes out CLEAN without a backfill.

Ingestion note: the Step-2 collapse was a parser artifact — if the dup SOP is
re-ingested, re-run `fix_duplicate_step2_rules.py` to restore the two routing
rows (or fix the table parse in `a03_parse_html`).
