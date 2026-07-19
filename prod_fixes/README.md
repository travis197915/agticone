# Prod Fixes — Auditor Issue Tracker Bundle

This folder bundles every **deterministic production fix** raised in the auditor
issue tracker (`Issue Tracking-Table 1.csv`) into one self-contained place. Each
sub-folder covers one *kind* of issue and contains:

- **the prod fix script(s)** — copied verbatim from `scripts/`, so the folder is
  self-contained (the scripts auto-discover the repo root, so they run from
  anywhere in the repo).
- **`README.md`** — the bug (as the auditor reported it), the verified root
  cause, exactly what the script changes, how to run it on prod, what to verify
  in the UI, and **the execution-engine fix** for that class of issue (which
  file/function, and whether it is already shipped or still needs to land).

## Ground rules that every script here obeys

- **No LLM, no network, no re-run.** Every fix writes straight into the Postgres
  the dashboard reads (`RuleEvaluation`, `RuleExecutionRun`, `ClaimTrace`,
  `ClaimExecutiveSummary`, and workflow definition tables). No Anthropic/OpenAI
  call, no MCP/tool call, no re-execution of the LangGraph pipeline.
- **`--dry-run` is the default; `--apply` writes.** Every script prints the
  resolved DB target banner first, previews per-claim changes on `--dry-run`,
  and only commits under `--apply`.
- **Idempotent.** Re-running a script after it has been applied is a no-op —
  each carries a marker / signature check so already-fixed rows are skipped.
- **Verdict-safe.** A fix never regresses a clean claim into a defect. Scripts
  that could change a verdict recompute it with the same decision-type rollup
  the dashboard uses, and the step-10 fix additionally rolls back unless the
  claim recomputes to fully `ALLOW`/`CLEAN`.
- **PROD DB is baked in** (private-link Postgres) and overridable by any `PG_*`
  env var, so the same file runs on the prod box (bare) or the local
  prod-replica (`PG_HOST=127.0.0.1 PG_PORT=5433 …`).

Default workflow id used by all backfills: `7c476f09-5196-438f-b25e-9cc3c96eac97`.

## Recommended run order on prod

Definition/forward fixes first (so future runs are correct), then the per-claim
backfills. Within a folder the README lists the exact order.

```bash
# always preview first
python prod_fixes/<folder>/<script>.py --dry-run
# then commit
python prod_fixes/<folder>/<script>.py --apply
```

## Issue → folder → script map

| Tracker item(s) | Issue | Folder | Primary script | Engine fix status |
|---|---|---|---|---|
| 1, 4, 13 | OBH Facets duplicate claim handling (Step 2 criteria; Step 7 CDD "no evidence" reasoning) | `01_duplicate_claim_handling` | `seed_duplicate_cdd_clean_prod.py` | Shipped (SYSTEM disposition + shared tool) |
| 9 | Timely filing must run on ALL claims (drop the TF0/TF1 gate → Step 1 Not Applicable) | `02_timely_filing_run_on_all_claims` | `scope_timely_filing_na_prod.py` | Shipped (manual OOS / scope) |
| 37 | Timely filing Step 5 Adjustments/Appeals is out of scope | `03_timely_filing_adjustments_oos` | `mark_timely_filing_adjustments_oos_prod.py` | Shipped (`manual_oos_rule_keys`) |
| 38 | Timely filing Step 10 shows a false defect (no claim in history, correctly denied) | `04_timely_filing_step10_no_history` | `fix_tf_step10_no_history_defect_prod.py` | **Needs engine change** (routing branch Not-Met → DEFECT; DENY-confirm counts adverse) |
| 12, 36 | CBD Coverage: covered codes shown as "not found" | `05_cbd_not_found_covered` | `fix_coverage_not_found_covered_prod.py` | **Needs engine change** (CBD grid keys on descCode, not cptCode) |
| 17, 35 | Medicare coverage tool fired on Commercial; "Standard Commercial" CBD path | `06_medicare_coverage_tool_to_cbd` | `scope_medicare_coverage_tool_prod.py`, `fix_cbd_plan_path_prod.py` | Shipped (per-tool `lob_scope`) |
| 14, 24, 39 | Subscriber ID marked not-matched when it is the same identity | `07_subscriber_id_match` | `fix_subscriber_id_match_prod.py` | Partial (identity normalization; Additional-ID field not exposed by tools) |
| 16, 19 | Process-action steps (F3/F4 "Process the claim") should be Not Applicable | `08_process_action_not_applicable` | `mark_process_action_na_prod.py` | **Needs engine change** (add process-action detector to Pass-1 skips) |
| 34 | Provider Selection group-model routing evaluated AND instead of OR (2A/2I) | `09_provider_selection_group_model_or` | `fix_provsel_group_model_or_prod.py` | Shipped (definition rewrite; `condition_and` cleared) |
| 33 | Provider Selection non-selected branch (Steps 5/6) shows Clean, should be Not Applicable | `10_provider_selection_branch_na` | `mark_provider_selection_branch_na_prod.py` | **Needs engine change** (non-selected branch header rolls up to CLEAN) |
| 23 | Provider Selection Step 7 (3B) rule #172 marked not-matched, should be matched | `11_provider_selection_step7_3b_172` | `apply_provsel_172_fix_prod.py` | Data/definition fix (5th-choice ladder) |
| 18, 25–31 | Physician Claim Checklist mandatory fields (COB, Auth, Type of Bill, Copay, …) out of scope | `12_physician_checklist_oos_fields` | `mark_physician_checklist_oos_prod.py`, `add_physician_checklist_fields_prod.py` | Shipped (`manual_oos_rule_keys` + full field set) |
| — (LOB) | Medicare-only SOPs (NPI check, Provider Opt-Out) must not run on non-Medicare | `13_lob_gating_medicare_steps` | `scope_medicare_steps_prod.py` | Shipped (`Workbench.config['lob_scope']` + `_rule_in_lob_scope`) |

## Full tracker status — all 40 items

Every row of `Issue Tracking-Table 1.csv`, its tracker status, and what we did.

### SOP / CBD bugs — scripted & bundled

| # | Claim | Issue | Tracker status | Our fix |
|---|---|---|---|---|
| 1 | 25XK20940100 | Dup handling – Step 2 missing Member/DOS/Procedure criteria | CLOSED | `01_duplicate_claim_handling` (`fix_duplicate_step2_rules.py`) |
| 4 | 25XK20940100 | Dup Step 7 – "no evidence of CDD edit" | CLOSED (caching) | `01` (`seed_duplicate_cdd_clean_prod.py`) |
| 13 | 25XK20940100 | Dup Step 7 – agent doesn't agree CDD denial | CLOSED "will add reasoning" | `01` |
| 9 | 25XK20940100 | Timely filing must run on ALL claims; Step 1 → N/A | DONE (auditor verify) | `02_timely_filing_run_on_all_claims` |
| 37 | 25XJ83640500 | TF Step 5 Adjustments/Appeals out of scope | open | `03_timely_filing_adjustments_oos` |
| 38 | 25XJ83640500 | TF Step 10 false defect (no history, correctly denied) | open | `04_timely_filing_step10_no_history` |
| 12 | 25XK20940100 | CBD 90837 "not found" but is covered | DONE→reopened (wording) | `05_cbd_not_found_covered` |
| 36 | 25XJ87029800 / 25XK07904100 | 99214 & 90833 not found but covered | OPEN | `05` |
| 17 | 25XJ46879400 | Commercial claim referencing Medicare coverage tool → CBD | DONE (auditor verify) | `06_medicare_coverage_tool_to_cbd` |
| 35 | 25XJ87029800 | CBD path should be "Avmed > Commercial" not "Standard Commercial" | OPEN | `06` (`fix_cbd_plan_path_prod.py`) |
| 14 | 25XJ46879400 | Subscriber ID matches (3 Facets fields) but marked not-matched | CLOSED | `07_subscriber_id_match` |
| 24 | 25XK05953100 | Subscriber ID mismatch should be an Error | DONE (auditor verify) | `07` (leaves unprovable pairs as a finding) |
| 39 | 25XJ57727400 | Subscriber ID match on Additional-ID screen | open | `07` (`--attested`) |
| 16 | 25XK20940100 | F3 process-action step → Not Applicable | CLOSED | `08_process_action_not_applicable` |
| 19 | 25XK05953100 | Process action across all SOPs → N/A (reopened; also Proc 2 step 14) | DONE (auditor verify) | `08` |
| 34 | 25XJ88210600 | Group model 2A/2I evaluated as AND, should be OR | (cmd in CSV) | `09_provider_selection_group_model_or` |
| 33 | 25XJ46879400 | Provider Steps 5 & 6 marked clean, should be N/A | DONE (auditor verify) | `10_provider_selection_branch_na` |
| 23 | 25XK02420100 | Provider Step 7 3B rule #172 not-matched, should match | CLOSED | `11_provider_selection_step7_3b_172` |
| 18 | — | Physician checklist mandatory on every claim / SOP link | DONE (auditor verify) | `12_physician_checklist_oos_fields` (`add_…fields`) |
| 25 | 25XJ88210600 | COB (#227) out of scope | DONE (auditor verify) | `12` |
| 26 | 25XJ88210600 | Authorization (#229) out of scope | DONE (auditor verify) | `12` |
| 27 | 25XJ88210600 | Statement Covers Period (#229) out of scope | DONE (auditor verify) | `12` |
| 28 | 25XJ88210600 | Type of Bill (#232) out of scope | DONE (auditor verify) | `12` |
| 29 | 25XJ88210600 | Copay (#234) out of scope | DONE (auditor verify) | `12` |
| 30 | 25XJ88210600 | Coinsurance (#235) out of scope | DONE (auditor verify) | `12` |
| 31 | 25XJ88210600 | Deductible (#236) + Accommodation Rates (#233) out of scope | DONE (auditor verify) | `12` |
| — | LOB thread | Medicare opt-out / NPI check must not run on non-Medicare | — | `13_lob_gating_medicare_steps` |

### Closed by auditors — no script needed

- **2** — Dup Step 7 corrected EOB / COB out of scope → "No issue, caching."
- **3** — Dup Step 7 UB / facility claim out of scope → "This is out of scope."
- **10** — "Issue Resolved – we can delete this issue."
- **15** — Pre-Step exceptions scope → "No issue, just clarification."

### Portal / product features — NOT scripted (no deterministic DB fix)

- **5** caching/refresh · **6** summary too long · **7** SOP name + link ·
  **8** status labels (N/A / Out of Scope / Clean / Defect) · **11** SSO/OIDC
  (in progress) · **20** HTML SOP view · **21** reviewer fields ·
  **22** ERA "AuditorName" extraction · **32** 2nd-reviewer backfill for
  pre-existing claims.

### Still open — needs clarification

- **40** — 25XJ14948000, "Claim status 91 – Adjusted; adjusted claims out of
  scope for POC." The CSV itself says **"Need clarification."** No script yet —
  needs the rule for detecting + presenting an adjusted claim.

### Bottom line

- **26 SOP/CBD bug items + the LOB thread → fully bundled** (script + README +
  engine-fix doc) in `prod_fixes/`.
- **4 closed** by auditors (2, 3, 10, 15) — no work.
- **9 portal/feature** items (5, 6, 7, 8, 11, 20, 21, 22, 32) — out of scope for scripts.
- **1 (#40)** blocked on clarification.

Caveats (also in each folder's README): items **04, 05, 08, 10** are corrected on
existing runs by the backfill but the **engine still reproduces them on new runs**
until the documented engine change lands; **24/39** subscriber-ID relies on
auditor attestation for the Facets Additional-ID field our tools don't expose.

## Engine fixes still to land (see each folder's README for detail)

The classes below are corrected on existing runs by the backfill scripts, but
the **engine still emits them on new runs** until the code change lands:

1. **Timely Filing Step 10 false defect** (`04_…`) — a non-applicable routing
   branch renders `Not-Met` and `trace_builder.claim_status` rolls any `Not-Met`
   up to DEFECT; the "allow the system to deny for timely filing" confirm row is
   `decision_type=DENY` and counts as an adverse finding.
2. **CBD not-found vs covered** (`05_…`) — the `cbd_coverage` grid rows carry an
   empty `cptCode`, so the naive not-found bucket flags every queried CPT.
3. **Process-action steps** (`08_…`) — no deterministic Pass-1 skip exists for
   F3/F4 keystroke macro steps; they are LLM-evaluated and marked Met.
4. **Provider Selection non-selected branch** (`10_…`) — a non-selected
   group-model branch header stays `matched=False, skipped=False`, and
   `node_audit_status` treats any non-skipped row as executed → rolls up to
   CLEAN.
