# Provider-Selection False Positives — Root Cause & Context Fixes

> Why the **Complete Claim Audit Pipeline** flagged a set of claims as `DEFECT`
> that human auditors had marked **CLEAN**, what the real causes were, and the
> context-engineering work we did to make the execution engine reason the way an
> auditor does — all of it running **at execution time** and **editable from the
> UI**.

Companion to [`complete-claim-audit-defect-study.md`](./complete-claim-audit-defect-study.md)
(the per-claim study). This README is the architectural + remediation narrative.

---

## 1. TL;DR

- A batch of claims came back `DEFECT` even though auditors said `CLEAN`.
- The defects nearly all collapsed to **one rule family**: **SOP-5 Provider
  Selection** (`ProviderSelectionVerification`), firing a 3rd/4th/5th-choice
  `DENY`.
- The `DENY` *labels are correct per the gold SOP* — the engine was **selecting
  the wrong choice** because it was missing three pieces of business context:
  1. **INN/OON was read literally** off `CLCL_NTWK_IND` / the facet-extension
     `group_model`, instead of being **derived** via the 2-point / 3-point
     provider match the SOP requires.
  2. **"Individual is billed"** was being satisfied by **DOC360 box 27**
     (accept-assignment / assignment-of-benefits), which is *not* an
     individual-billed signal. The real signal is **box 24 (Rendering NPI)** /
     **box 33 (Servicing Physician Name)**.
  3. **Group-model gating was lost** in IR flattening — a claim with
     `group_model='AN'` (step 4 table) matched a **2A/2I** choice (step 5 table).
- We also found the field-mapping config (`yaml/sop_field_mapping.yaml`) was
  **gitignored and never deployed**, so the mapping mechanism was inert in prod.
- **What we did:** moved the field mapping + claim ontology into the **database**
  (editable from the UI, live-reloaded across processes), made the ontology
  **actively participate** in field resolution via alias expansion, added the
  missing individual-provider canonical fields, and injected a **provider-
  selection domain-guidance block** into the rule-eval prompt that re-teaches the
  determination procedure and field semantics. All of this lives in **code/DB
  that deploys**, not in gitignored YAML.

---

## 2. How the execution engine decides (context)

The **Complete Claim Audit Pipeline** workflow runs ~62 shapes / **147 decision
rules** per claim. Each rule is evaluated by a **single LLM call** (Claude
Sonnet) built in
[`uhc-execution-engine/src/uhc_execution_engine/agents/_eval_common.py`](./uhc-execution-engine/src/uhc_execution_engine/agents/_eval_common.py).
The prompt is assembled from:

```
RULE          key / source / section / decision_type / condition / action
MAPPED FIELDS canonical SOP fields resolved from claim + tool results
SOP ROUTING   step / goto / out-of-scope / applicable_when
DOMAIN GUIDANCE   ← NEW: determination procedures + field semantics (scoped)
CLAIM         the parsed claim JSON
TOOL RESULTS  FACETS / DOC360 / NPI / coverage tool outputs (compacted)
```

The LLM returns `{matched, reasoning, confidence, status, applicable,
navigation, evidence_refs, conditions}`. A rule that **matches** with a
`decision_type` in `{DENY, STOP, REFER, PEND}` is a **defect**
(see `_DEFECT_DECISIONS` in `execution_app/trace_builder.py`).

**Key insight:** the defects are **prompt/context failures, not model-capability
failures**. Sonnet can clearly reason this out — it was being fed the wrong /
missing signals. That is why the fix is **context engineering**, not
fine-tuning.

---

## 3. Root cause (corrected after live tool-call verification)

The first pass mislabeled SOP-5's 3rd/4th/5th-choice rows as a "label bug"
(`DENY` that should be `CONDITIONAL`). Reading the gold
[`yaml/ProviderSelectionVerification.yaml`](./yaml/ProviderSelectionVerification.yaml)
shows that is **wrong**: those choices explicitly say *"raise Provider Selection
error and claim should be denied."* `DENY` is the **intended** disposition. The
defect is that the engine **selects the wrong choice** when the correct one is
1st/2nd (clean).

### 3.1 INN/OON read at face value
Gold `RULE-000` says INN/OON must be **derived**:

> - Match the billed provider against the FACETS **provider-details** records.
> - **Multiple records** match on **2 points** (Tax ID/EIN + NPI) → **INN**.
> - **Exactly one record** matches on all **3 points** (Tax ID + NPI + name/addr) → **OON**.
> - Then **reconcile** with the network indicator.

The engine never ran this match. It took `CLCL_NTWK_IND='O'` / `group_model='AN'`
("**Assumed** Non-network" — an assumption, not a determination) literally,
concluded OON, and landed on a deny choice instead of 1st/2nd (clean).

### 3.2 "Individual is billed" had no dedicated field
[`field_mapping.py`](./uhc-execution-engine/src/uhc_execution_engine/field_mapping.py)
resolves with `_SYSTEM_ORDER = ("FACETS", "DOC360", …)`, so `Provider NPI` /
`Provider Name` always resolve to the **FACETS group** values. The DOC360
box-24 (Rendering NPI) / box-33 (Servicing Physician) **individual** signal was
**never surfaced as its own canonical field**, so the LLM guessed — in several
traces it cited DOC360 **box 27 ("A/ASSIGNED", "Y/YES ASSIGNED")**, which is
**assignment of benefits**, not a rendering provider.

### 3.3 Group-model gating lost in IR flattening
The gold SOP scopes each choice table by group model
(`table_name: "2A or 2I"`, etc.):

| group_model            | choice table | step |
|------------------------|--------------|------|
| 1A / AN / No Group Model | RULE-004     | 4    |
| 2A / 2I                | RULE-005     | 5    |
| 3A                     | RULE-006     | 6    |
| 3B                     | RULE-007     | 7    |

The IR flattens subrules into per-choice rows and **drops `table_name`**, so the
evaluator has no gate. A claim with `group_model='AN'` (step 4) matched a 2A/2I
3rd-choice rule (`step:5:5:2`).

### 3.4 SOP-8 genuine inversion (separate)
`RULE-003-001/002` (`AUDIT IS CORRECT`) is a **genuine** label inversion —
`DENY` that should be `ALLOW/CONDITIONAL`. Still pending (see §8).

---

## 4. Worked example — claim `25XJ81701500`

**Stored verdict:** `DEFECT` → rule `step:5:5:2` (SOP-5 "3rd choice", 2A/2I
table) matched `DENY @ conf 0.95`.

**Live tool data the run actually saw:**
- `facets_get_summary`: `PRPR_ID=GRP000064948` (a **group** id), `PRPR_ENTITY=G`,
  `PRPR_NAME="County of Sacramento"`, `NWNW_NAME="Not Assigned"`.
- `facet_ext_portal_group_model`: `group_model="AN"` (Assumed Non-network).
- DOC360 image: box 33 servicing physician present, box 27 `Y/YES ASSIGNED`.

**Why it falsely denied:** the LLM (a) accepted `group_model='AN'` as definitive
OON, (b) accepted box 27 "ASSIGNED" as "individual is billed", and (c) evaluated
a **2A/2I** rule on an **AN** claim. All three are the gaps in §3.

This is a clean **group-billed** claim; correctly evaluated it lands on the
1st/2nd choice.

---

## 5. What we built

### 5.1 DB-backed configuration (was gitignored YAML)
`yaml/` is in `.gitignore`, so `sop_field_mapping.yaml` and `claim_ontology.yaml`
were **never deployed** — the resolver silently returned empty in prod. We moved
both into Postgres so they deploy, run at execution, and are **UI-editable**.

| Model (`agent_tools/models.py`) | Table | Replaces |
|---|---|---|
| `SopFieldMapping` | `sop_field_mapping` | `yaml/sop_field_mapping.yaml` |
| `ClaimOntologyField` | `claim_ontology_field` | `yaml/claim_ontology.yaml` |

- **DB-first, YAML-fallback** loader in `field_mapping._load()` (keeps the
  standalone CLI working when Django/tables are absent).
- **Cross-process freshness** without restarts: a cheap `(count, max_updated_at)`
  **watermark** (`_db_watermark()`) invalidates the in-process `_DB_CACHE`; a
  `post_save`/`post_delete` **signal** (`agent_tools/signals.py`) eagerly clears
  the cache in the editing process, and Celery workers pick the change up via the
  watermark.
- **Seed command** `python manage.py seed_field_mapping` imports both YAMLs into
  the DB to bootstrap an environment.
- **REST CRUD** (`agent_tools/views.py` + `urls.py`): `…/field-mappings/`,
  `…/claim-ontology/`, plus a meta endpoint for the friendly system labels.
- **Frontend** (`claims-frontend`): context-driven pages under **Configuration**
  (`/config/field-mapping`, `/config/claim-ontology`) with friendly labels,
  search, grouping, and add/edit dialogs, so a non-engineer understands what the
  cryptic source keys (e.g. `"24 RENDERING NPI"`) mean.

### 5.2 New individual-provider canonical fields
Added to the mapping (DOC360-only, FACETS deliberately empty so the group record
can't shadow them):

- **`Rendering Individual Provider NPI`** → DOC360 `"24 RENDERING NPI"`
- **`Servicing Individual Provider Name`** → DOC360 `"33 SERVICING PHYSICIAN/SUPPLIER NAME, ADDRESS, PHONE"`

These give the LLM a real "individual is billed" signal separate from the
billing group.

### 5.3 Ontology participates at runtime (alias expansion)
`field_mapping.resolve_sop_fields()` now builds an `_alias_index_for(ontology)`
and uses `_expand_keys()` so a mapping's source key resolves even when the claim
image uses a different raw label for the same box. The ontology is no longer just
static documentation — it actively widens field resolution during execution.

### 5.4 Provider-selection domain guidance (the core context fix)
New in
[`_eval_common.py`](./uhc-execution-engine/src/uhc_execution_engine/agents/_eval_common.py):
`_domain_context(rule)` detects provider-selection rules (≥2 distinctive signals
in the rule text) and injects `_PROVSEL_GUIDANCE` into the prompt. It re-teaches:

1. **INN/OON is derived**, never read off `CLCL_NTWK_IND`/`group_model`; do the
   2-point (→INN) / 3-point (→OON) match, reconcile with the indicator, and mark
   **`Inconclusive`** (not OON) when the provider-details records are absent.
2. **"Individual is billed" = box 24 / box 33**; **box 27 is assignment of
   benefits — never use it** for the individual-billed condition.
3. **Group-model gating** (1A/AN/None→step4, 2A/2I→step5, 3A→step6, 3B→step7);
   set `applicable=false` when the rule's table ≠ the claim's `group_model`.
4. A group-billed **INN** claim selects the 1st/2nd choice and is **CLEAN**.

**Scoped & additive:** verified the block emits only for provider-selection rules
(timely-filing, eligibility, etc. are unchanged), so it does not bloat the other
~140 prompts. It lives in **code, which deploys** — so it actually runs in prod,
unlike the YAML.

### 5.5 Schema-drift fix (unblocked reruns)
The live `execution_rule_evaluation` table carried three columns the
`RuleEvaluation` model never declared — `live_result`, `overridden` (NOT NULL, no
default), `injected_context` — from a deployed override feature. Because the
model omitted `overridden`, **every insert raised `IntegrityError`** and aborted
runs at step 4. Fixed by adding the fields to the model + a **state-only**
migration (`execution_app/migrations/0005_ruleeval_override_state.py`) since the
columns already exist in the DB.

---

## 6. Verification

Targeted **before/after** test of the offending rule (`step:5:5:2`) on
`25XJ81701500`, reusing the real fetched tool data (one LLM call each):

| Variant | `matched` | Reasoning quality |
|---|---|---|
| Originally stored | `True` (DENY) | concluded OON from group_model; treated claim as individual-billed off the wrong box |
| **WITH guidance** | `False` (Not-Met) | cites **box 24 rendering NPI** (not box 27); flags `group_model='AN'` as an assumption; marks OON **Inconclusive** without provider-details → **deny does not fire** |

The false `DENY` no longer fires, and the reasoning now follows the auditor's
determination procedure.

---

## 7. How to operate

- **Edit context (no deploy):** Configuration → Field Mapping / Claim Ontology in
  the frontend. Saves hit `SopFieldMapping` / `ClaimOntologyField`; the engine
  picks changes up live (signal in-process, watermark cross-process).
- **Bootstrap a new env:** `PYTHONPATH=. python manage.py seed_field_mapping`.
- **Re-run a single claim** (≈25 min, 147 live LLM calls): `_rerun_one.py <claim_id>`.
- **Re-run the defect batch:** `_rerun_defect_claims.py` (long-running).
- Domain guidance is in code — change `_PROVSEL_GUIDANCE` / `_PROVSEL_SIGNALS` in
  `_eval_common.py` and redeploy.

---

## 8. Residual gaps / pending

1. **Provider-details fetch (data gap).** The 2-point/3-point match needs the
   FACETS **provider-details multi-record** section, which the workflow does not
   currently fetch. Until a tool binding supplies it, the guidance correctly
   stops the false `DENY` but can only mark OON **Inconclusive** — it cannot
   *positively* confirm INN to roll the claim fully CLEAN. **Recommended next
   step:** bind a provider-details tool for the provider-selection steps.
2. **SOP-8 `AUDIT IS CORRECT` inversion** (`RULE-003-001/002`): genuine label
   inversion `DENY` → `ALLOW/CONDITIONAL`. Still pending.
3. **Full-claim end-to-end rerun** of the defect set to quantify how many flip
   `DEFECT → CLEAN` after these changes (each claim ≈25 min).
4. **Eval harness** over auditor-labeled claims (precision/recall per SOP) so
   "how many flip clean" becomes a measured, regression-guarded number — and the
   scoreboard for any future tuning.

---

## 9. Why context, not fine-tuning

- The errors are **context-shaped, not capability-shaped** — the model reasons
  correctly once given the determination procedure and field semantics.
- Claims audit needs a **defensible reason string** per decision; the rule +
  context path keeps explainability that a fine-tuned black box would weaken.
- SOPs change; **DB-backed rules/mappings update instantly**, a fine-tuned model
  goes stale and needs re-training.
- Fine-tuning doesn't reduce the 147-calls/claim cost or latency.

Fine-tuning (SFT) stays an option **only** for a narrow rule family that
survives context tuning *and* has enough auditor-labeled volume — and only after
an eval harness exists to measure regression.

---

## 10. Change map

| Area | File(s) |
|---|---|
| Domain guidance (core fix) | `uhc-execution-engine/src/uhc_execution_engine/agents/_eval_common.py` |
| Field resolution + ontology alias expansion + DB-first loader | `uhc-execution-engine/src/uhc_execution_engine/field_mapping.py` |
| DB-backed config models | `agent_tools/models.py` (`SopFieldMapping`, `ClaimOntologyField`) |
| Migrations | `agent_tools/migrations/0006…`, `0007…`; `execution_app/migrations/0005_ruleeval_override_state.py` |
| Cache invalidation | `agent_tools/signals.py`, `agent_tools/apps.py` |
| Seed command | `agent_tools/management/commands/seed_field_mapping.py` |
| REST API | `agent_tools/serializers.py`, `views.py`, `urls.py` |
| New canonical fields | `yaml/sop_field_mapping.yaml` (+ seeded into DB) |
| Frontend config pages | `claims-frontend/src/routes/config/field-mapping`, `…/claim-ontology`, `lib/fieldMappingApi.ts`, `interfaces/fieldMapping.ts`, sidebar nav |
| Per-claim study | `complete-claim-audit-defect-study.md` |
| Rerun / test scripts | `_rerun_one.py`, `_rerun_defect_claims.py`, `_test_provsel_rule.py` |
