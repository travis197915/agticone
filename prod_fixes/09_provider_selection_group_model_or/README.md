# 09 — Provider Selection group-model routing: AND → OR (2A/2I)

**Tracker item:** 34 (SOP · Bug) — claim `25XJ88210600`, Process 4 Step 3, rule #152

## The bug (as auditors reported it)

> The condition requires **BOTH '2A' AND '2I'** to be present. Provider could be
> 2A **or** 2I — both do not need to be present. Rule should be **matched** as the
> provider is 2I. The remainder of the Group Models should be **Not Applicable**.

## Root cause (verified)

"OBH Facets Provider Selection Guidelines" Step 3 routes to a branch by the
claim's **group model**, via mutually-exclusive rows:

```
RULE-003-001  1A, AN, or No Group Model → Proceed to next step
RULE-003-002  2A, 2I                     → Skip to Step 5   (rule #152)
RULE-003-003  3A                         → Skip to Step 6
RULE-003-004  3B                         → Skip to Step 7
```

Each row stored the group-model set in **both** `condition_if` and
`condition_and`, and the engine renders `condition_if AND condition_and`
(`rule_loader._hydrate_decision`, line ~122):

```
"2A, 2I from facet extension portal. AND 2A, 2I"
```

The evaluator read the trailing `AND 2A, 2I` as an **additional** requirement and
concluded the claim must be **both** 2A and 2I. A group model is a single value,
so the row could **never** match — "2A, 2I" was meant as a membership set (2A OR
2I). The buggy "AND" text lives in the baked `NodeRuleBinding.condition`, which
the engine prefers over the live SOP row, so the fix must rewrite the binding
(and the `AuditDecision` for consistency).

## The fix

`fix_provsel_group_model_or_prod.py` — deterministic, no-LLM, idempotent.

- **Phase A — definition (future runs):** for every group-model routing row (one
  whose `condition_and` is purely group-model tokens), rewrite
  `NodeRuleBinding.condition` and `AuditDecision.condition_if` to explicit
  OR/membership phrasing and **clear `AuditDecision.condition_and`** so no
  "AND &lt;set&gt;" is emitted. Non group-model rows (entity-locate rows 0/1) are
  never touched. A `_REWRITE_MARKER` makes route detection robust to both the
  original and rewritten forms.
- **Phase B — backfill (existing runs):** read the claim's group model from the
  `facet_ext_portal_group_model` tool result (authoritative; falls back to a
  **majority vote** over routing-eval reasoning when the tool result is empty, so
  it converges in one pass). Then:
  - the matching routing row → `matched=True` (corrected reasoning + OR text),
  - the others → **Not Applicable** (`skipped=True, matched=False`).
  Kept in sync across `RuleEvaluation`, `ClaimTrace` (routing shape entry + its
  sub-rules), and `ClaimExecutiveSummary`. Verdict recomputed defensively
  (routing rows are non-adverse CONDITIONAL, so it can never regress).
- **Halt guard:** claims where Provider Selection was skipped upstream (out of
  scope / auditing stopped) are left untouched — no fabricated match.

## Run on prod

```bash
python prod_fixes/09_provider_selection_group_model_or/fix_provsel_group_model_or_prod.py --dry-run
python prod_fixes/09_provider_selection_group_model_or/fix_provsel_group_model_or_prod.py --apply
```

## Verify in the UI (25XJ88210600, group model 2I)

- **Process 4 · Provider Selection · Step 3**, rule #152 (2A/2I) → **Matched**,
  reasoning reads *"group model 2I is a member of {2A, 2I}"*.
- The other group-model rows (1A/AN, 3A, 3B) → **Not Applicable**.
- Verdict unchanged.

## Execution-engine fix (status: shipped — definition-level)

The durable fix is the Phase-A rewrite: the group-model routing rows now carry
OR/membership phrasing in `condition_if` and an empty `condition_and`, so
`rule_loader` no longer emits `... AND <set>` (the join at `rule_loader.py`
line 122 only concatenates non-empty parts). Any **new** run evaluates the row as
a membership test and matches on a single group-model value.

Optional hardening at the ingestion source: a group-model "set" column (comma-
separated tokens) should be parsed into an OR/membership condition, not
duplicated into `condition_and` — see `a03_parse_html` / `a07_enrich` where
`condition_and` is populated. Until that lands, re-running Phase A after a
re-ingest restores the correct phrasing.
