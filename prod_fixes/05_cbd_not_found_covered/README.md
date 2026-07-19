# 05 — CBD Coverage: covered codes shown as "not found"

**Tracker items:** 12, 36 (CBD / SOP · Bug) — claims `25XK20940100`,
`25XJ87029800`, `25XK07904100` (Process 8, rules #218/#219)

## The bug (as auditors reported it)

> Rule #218 — *"the code was not found in coverage records"*. **90837 does
> reflect "covered" in CBD.** Why is the agent stating "not found"? Rule #219
> even notes *"'not found' is a valid coverage category, not a failure."* So
> "not found" means covered? (Item 36: 99214 & 90833 reflect not found **and**
> covered — the codes ARE covered.)

## Root cause (verified)

The Coverage/Benefit SOP ("Access Covered Benefit SOP", Process 8, sop_id 17)
steps 3/4/5 categorize each procedure code with the `cbd_coverage` tool.
`cbd_coverage` returns the plan's benefit **grid**, whose rows carry an **empty
`cptCode`** (the CBD API keys on `descCode`/`descName`, not the literal CPT). The
naive not-found bucket therefore catches **every** queried CPT, so the agent's
`rationale` / `evidence_refs` / eval `reasoning` say the codes are
`not_found_codes` — even though the **same step's deterministic sub-rule
statement** already correctly reads *"CPT &lt;code&gt; — Covered under the applicable
Covered Benefit Document benefit"*.

The persisted step is internally **contradictory**: statement = **Covered**,
narrative = **not found**. The auditor reads the "not found" wording and (rightly)
flags it. The verdict is already CLEAN/ALLOW (covered → no defect); this is a
**narrative-accuracy** correction, not a verdict change.

## The fix

`fix_coverage_not_found_covered_prod.py` — deterministic, no-LLM, in place. Only
runs whose step-3 statement says at least one code is **Covered** AND whose
narrative still says **not found** are touched (genuinely not-covered/not-found
codes are left alone). Per claim's latest run, for the Access Covered Benefit SOP
steps 3/4/5:

1. Parse the authoritative per-code determination from the step-3 sub-rule
   `statement` (`CPT <code> — Covered | Not covered | Not found`).
2. Rewrite eval `reasoning` (`step:17:{3,4,5}:*`) + trace `rationale` to the TRUE
   determination (covered), removing the false "not found" wording.
3. Rewrite `evidence_refs` + nested `subrule_results[].conditions[]`
   (`not_found_codes` → `covered_codes`, `codes_found=0` → the real count,
   `'not found'` → `'covered'`).
4. Sub-rule `statement`/`status` (Met) left as-is (already correct);
   `matched`/`decision_type`/verdict unchanged (stays CLEAN). `final_status` +
   `explainability_json` recomputed.

`check_coverage_not_found_prod.py` is a read-only reporter to find affected runs.

## Run on prod

```bash
python prod_fixes/05_cbd_not_found_covered/check_coverage_not_found_prod.py          # report
python prod_fixes/05_cbd_not_found_covered/fix_coverage_not_found_covered_prod.py --dry-run
python prod_fixes/05_cbd_not_found_covered/fix_coverage_not_found_covered_prod.py --apply
```

## Verify in the UI

- **Process 8 · Coverage/Benefit · Steps 3/4/5**, rules #218/#219/#220 — the
  rationale and evidence now say the codes (e.g. 90837, 99214, 90833) are
  **covered under the Covered Benefit Document**; no "not found" wording.
- Claim stays **CLEAN / ALLOW**.

## Execution-engine fix (status: NEEDS ENGINE CHANGE)

New runs still emit the "not found" narrative until the tool-output parsing is
corrected. The engine change is in how the coverage step buckets codes from the
`cbd_coverage` result:

- The `cbd_coverage` grid rows key on `descCode`/`descName`, **not** `cptCode`
  (which is empty). Match a queried CPT to a grid row by its description mapping
  (or treat the grid as a whole-plan "covered" list) instead of exact
  `cptCode` equality. A CPT should land in `covered_codes` when it maps to a grid
  row, and only in `not_found_codes` when there is genuinely no coverage entry.
- This lives in the coverage-determination evaluation the SOP-17 step uses (the
  code that builds `not_found_codes`/`covered_codes` from the tool payload — see
  `enrich_coverage_determination.py` for the same bucketing logic used offline,
  and the step-17 prompt/tool-context assembly in `_eval_common`). Fixing the
  bucketing there makes the deterministic sub-rule statement and the LLM
  narrative agree, so the contradiction never appears on a new run.
