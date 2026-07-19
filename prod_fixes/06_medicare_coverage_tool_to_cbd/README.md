# 06 — Medicare coverage tool on Commercial claims → CBD

**Tracker items:** 17, 35 (SOP · Improvement/Bug) — claims `25XJ46879400`,
`25XJ87029800` (Process 8, Step 2/3/4, rules #218/#219/#220)

## The bug (as auditors reported it)

- **Item 17:** *"The Medicare coverage tool was called with procedure code G2074
  … Claim is Commercial, why is the agent referencing a Medicare coverage tool?
  Possible update to Medicare tool → Covered Benefit Document (CBD)."* Auditor
  re-test: verbiage must say **CBD Coverage Tool**, not Medicare coverage.
- **Item 35:** *"Agent reflects CBD for **Standard Commercial** was selected. CBD
  path should be **Avmed &gt; Commercial**."*

## Root cause (verified)

The Coverage/Benefit SOP binds **two** coverage tools on the same step:
`cbd_coverage` (all LOBs) and `check_medicare_coverage` (**Medicare-only**). The
Medicare tool carried **no LOB scope**, so it ran for every claim — including
Commercial. Worse, for non-Medicare claims `check_medicare_coverage` returns a
hardcoded stub `{"plan_name": "Standard Commercial", "group_name": "Standard
Commercial", "codes_found": 0}`, which the agent then surfaced as the CBD plan
name. "Standard Commercial" is **not a real CBD selection**; the real
`cbd_coverage` endpoint carries no plan/path field, so the correct path must be
derived from the claim's Facets data (`<Payer> > <LOB>`, e.g. `Avmed >
Commercial`).

The Coverage/Benefit SOP is valid for every LOB (unlike the NPI / Opt-Out SOPs in
folder 13), so the fix is at the **tool** level: keep the step and `cbd_coverage`,
drop the Medicare tool for non-Medicare claims. Every shape binding
`check_medicare_coverage` also binds `cbd_coverage`, so nothing is lost.

## The fix (run in this order)

### 1. `scope_medicare_coverage_tool_prod.py` — the forward + backfill fix

- **Phase A — tool tagging:** set
  `NodeToolBinding.args_template['_lob_scope'] = ['Medicare']` on the
  `check_medicare_coverage` bindings. (The engine already defaults this tool to
  Medicare — see below — but the explicit tag makes it visible in the builder and
  survives a rename.)
- **Phase B — backfill (non-Medicare runs):** move `check_medicare_coverage` from
  `tools_used` → `tools_skipped` on every step; reattribute the Medicare-coverage
  references in trace rationale / sub-rules / `RuleEvaluation.reasoning` /
  `ClaimExecutiveSummary` to **CBD** (deterministic regex, in place — no regen,
  no LLM). Step verdicts unchanged (coverage already came via `cbd_coverage`).

### 2. `fix_cbd_plan_path_prod.py` — repair the "Standard Commercial" text

Replaces the stub `Standard Commercial` plan name with the claim's real
`<Payer> > <LOB>` path (payer from Facets `PLDS_DESC`/`PDDS_DESC` leading token;
LOB from `run.claim_lob['product']`). Patches `ClaimTrace`, `RuleEvaluation`,
`ClaimExecutiveSummary`, and the stored `ToolInvocationRecord.plan_name`/
`group_name`. Claims whose payer cannot be confidently mapped are **skipped and
listed** (extend `PAYER_MAP`) — never fabricates a path.

## Run on prod

```bash
# forward-scope + reattribute to CBD (run first)
python prod_fixes/06_medicare_coverage_tool_to_cbd/scope_medicare_coverage_tool_prod.py --dry-run
python prod_fixes/06_medicare_coverage_tool_to_cbd/scope_medicare_coverage_tool_prod.py --apply
# then repair the residual "Standard Commercial" path text
python prod_fixes/06_medicare_coverage_tool_to_cbd/fix_cbd_plan_path_prod.py --dry-run
python prod_fixes/06_medicare_coverage_tool_to_cbd/fix_cbd_plan_path_prod.py --apply
```

## Verify in the UI (25XJ46879400 Commercial, 25XJ87029800)

- **Process 8 · Coverage/Benefit** — the tool list shows `cbd_coverage` used and
  `check_medicare_coverage` under **skipped** (greyed). Rules #218/#219/#220 read
  **CBD Coverage Tool**, not "Medicare coverage tool".
- CBD path reads **`Avmed > Commercial`** (or the claim's real payer/LOB), not
  "Standard Commercial".
- Verdict unchanged.

## Execution-engine fix (status: shipped — per-tool LOB gating)

Per-tool LOB gating is live end-to-end:

- `lob.py` (lines 38/46/155): `MEDICARE_ONLY_TOOLS` set,
  `default_tool_lob_scope(tool_name)` (defaults `check_medicare_coverage` etc. to
  `['Medicare']`), and `tool_in_lob_scope(lob_scope, product, label)`.
- `rule_loader.py`: tags each tool binding with an `lob_scope`.
- `n03_run_tools.py` (`_lob_skip_record`, line 30) and `n_execute_shapes.py`
  (`_ensure_tools`): a tool out of LOB scope is **not invoked** — a skip record is
  recorded instead.
- `_eval_common.py`: skipped tool output is kept **out of the LLM prompt**.
- `trace_builder.py`: skipped tools are listed under `tools_skipped`.
- `n07_persist_respond.py`: skipped tools are excluded from `ToolInvocationRecord`.

So a **new** non-Medicare run never invokes the Medicare coverage tool and never
emits the "Standard Commercial" stub — the two scripts only repair runs executed
before this landed.
