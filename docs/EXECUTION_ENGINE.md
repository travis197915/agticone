# Execution Engine — Change Log & Integration Guide

The execution engine takes an **Excel of claim identifiers** plus a
**workflow id** and produces a **per-claim adjudication** (DENY / ALLOW /
PEND / …) by fetching each claim through `linx_claim_search`, loading every
SOP rule attached to that workflow, and evaluating each rule against the
claim with an LLM.

It ships as two new components, both added in this change set:

- **[`uhc-execution-engine/`](../uhc-execution-engine/)** — standalone
  editable Python package containing the LangGraph pipeline and all
  evaluation logic. Mirrors the layout of
  [`uhc-api-agent/`](../uhc-api-agent/).
- **[`execution_app/`](../execution_app/)** — thin Django app that exposes
  the REST surface (`/api/execute/...`) and owns the persistence tables for
  run history and audit trail.

This guide covers what was added, how the pieces fit together, and how to
exercise the engine end-to-end.

---

## 1. Architecture at a glance

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Client (SPA, Postman, curl)                                              │
│   • Uploads an .xlsx of claim ids                                        │
└────────────┬─────────────────────────────────────────────────────────────┘
             │ POST /api/execute/workflows/<id>/run-batch/  (multipart)
             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ Django (sop_backend) :8000                                               │
│   • execution_app.views.RunBatchView                                     │
│   • Reads .xlsx → hands bytes + workflow_id to BatchRunner               │
└────────────┬─────────────────────────────────────────────────────────────┘
             │
             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ uhc_execution_engine.BatchRunner   (outer layer, plain Python)           │
│   parse xlsx → for each claim_id:                                        │
│     ├── linx_claim_search          (fetch claim form)                    │
│     ├── llm_parse_claim_with_ontology  (optional — only if bound on wf)  │
│     └── RuleEnginePipeline.run(...)    (inner 6-node LangGraph)          │
│   → batch summary                                                        │
└────────────┬─────────────────────────────────────────────────────────────┘
             │
             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ Inner LangGraph (6 nodes — per-Shape iteration)                          │
│   validate_input → load_bindings → run_tools →                           │
│   execute_shapes  →  aggregate_decision → persist_and_respond            │
│                                                                          │
│   execute_shapes iterates the workflow's Shapes in canvas order;         │
│   for each Shape it evaluates every attached rule (preconditions +       │
│   decisions) against the claim with that Shape's tool results in         │
│   context. If any rule on a Shape matches with decision_type             │
│   DENY/STOP → the claim is halted early (status=TERMINATED_EARLY).       │
└────────────┬─────────────────────────────────────────────────────────────┘
             │
             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ Postgres                                                                 │
│   • execution_batch_run        (one per upload)                          │
│   • execution_rule_run         (one per claim)                           │
│   • execution_rule_evaluation  (one per rule)                            │
│   • execution_tool_invocation  (one per tool call)                       │
│   FKs back into agent_tools.NodeRuleBinding / NodeToolBinding (SET_NULL) │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 2. What was added

### 2.1 New editable package — `uhc-execution-engine/`

```
uhc-execution-engine/
├── pyproject.toml
├── README.md
└── src/uhc_execution_engine/
    ├── __init__.py            # exports RuleEnginePipeline, BatchRunner
    ├── config.py              # .env-driven EngineConfig (model + key)
    ├── state.py               # ExecutionState TypedDict (graph state)
    ├── pipeline.py            # RuleEnginePipeline.run(workflow_id, claim)
    ├── batch.py               # BatchRunner.run_xlsx(...)
    ├── graph.py               # 7-node LangGraph wiring
    ├── llm.py                 # dual-provider _llm_call (Anthropic ↔ OpenAI)
    ├── rule_loader.py         # NodeRuleBinding/NodeToolBinding → rule dicts
    ├── tool_runner.py         # invoke a StructuredTool via agent_tools.registry
    ├── claim_fetcher.py       # linx_claim_search + parse helpers
    ├── xlsx_parser.py         # openpyxl: extract the claim-id column
    └── agents/
        ├── __init__.py
        ├── _eval_common.py    # shared per-rule LLM evaluation helper
        ├── n01_validate.py
        ├── n02_load_bindings.py
        ├── n03_run_tools.py
        ├── n04_preconditions.py
        ├── n05_decisions.py
        ├── n06_aggregate.py
        └── n07_persist_respond.py
```

Installed editably into the `uhc-agentic-backend` conda env:

```bash
conda activate uhc-agentic-backend
pip install -e ./uhc-execution-engine
```

### 2.2 New Django app — `execution_app/`

```
execution_app/
├── __init__.py
├── apps.py                    # ExecutionAgentConfig
├── admin.py                   # admin pages for all 4 tables
├── models.py                  # BatchExecutionRun, RuleExecutionRun,
│                              # RuleEvaluation, ToolInvocationRecord
├── serializers.py             # DRF serializers for the GET endpoints
├── views.py                   # RunBatchView, BatchDetailView, RunDetailView
├── urls.py
└── migrations/__init__.py     # (migrations NOT yet generated — see §6)
```

### 2.3 Modified files

| File | Change |
|---|---|
| [`sop_backend/settings.py`](../sop_backend/settings.py) | Added `"execution_app"` to `INSTALLED_APPS`. |
| [`sop_backend/urls.py`](../sop_backend/urls.py) | Mounted `"api/execute/"` → `execution_app.urls`. |
| [`requirements.txt`](../requirements.txt) | Added `openpyxl>=3.1`, `langchain-anthropic>=0.2`, `langchain-openai>=0.2`, and `-e ./uhc-execution-engine`. |

---

## 3. The 6-node inner pipeline (v2 — per-Shape iteration)

| # | Node | Job |
|---|------|-----|
| 1 | `validate_input` | Sanity-check claim + `workflow_id`; mint `run_id` (uuid). |
| 2 | `load_bindings` | Query `NodeRuleBinding` + `NodeToolBinding` for the workflow. Hydrate each rule against the live SOP graph the same way [`builder/views.py:212-288`](../builder/views.py#L212-L288) does — `condition`/`action` from the binding when overridden, otherwise from the authoritative SOP row. Output now includes a `shapes` list grouping rules + tool bindings by canvas Shape, ordered by `(workbench.order, shape.order)`. |
| 3 | `run_tools` | Invoke every `NodeToolBinding` (except the fetch/parse tools already handled in the outer layer) via `agent_tools.registry.get_tool()`. Failures are recorded but never abort. |
| 4 | `execute_shapes` | Iterate `state["shapes"]` in canvas order. For each Shape, run one LLM call per attached rule (preconditions and decisions together) with that Shape's tool results in context. If any matched rule on the current Shape has `decision_type ∈ {DENY, STOP}` → set `status=TERMINATED_EARLY`, record `terminated_at_shape_id`, and stop iterating. Otherwise continue to the next Shape. |
| 5 | `aggregate_decision` | One LLM call given the matched-rule list from `rule_results`. Precedence `DENY > STOP > PEND > REFER > BYPASS > WAIVE > CONDITIONAL > SYSTEM > ALLOW`. Dedupes codes; surfaces conflicts in the narrative. Early-halted claims short-circuit straight to a synthetic summary from the offending rule (no LLM call). Falls back to a deterministic precedence pick if the LLM call fails. |
| 6 | `persist_and_respond` | Inserts `RuleExecutionRun` + `RuleEvaluation` rows + `ToolInvocationRecord` rows; builds the response dict. Each evaluation row now carries `shape_id` + `shape_label` for the audit trail. |

> **Migration note (v1 → v2)**: the old `evaluate_preconditions` + `evaluate_decisions` nodes have been removed from the graph but kept importable from `uhc_execution_engine.agents` for one release so external callers don't break. They will be deleted in a follow-up.

Every LLM call goes through `uhc_execution_engine.llm.llm_call`, an adapted
copy of the dual-provider/retry helper from
[`a07_enrich.py:114-210`](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a07_enrich.py#L114-L210)
(Anthropic primary, OpenAI fallback, schema-validated, retried twice on the
primary provider before crossing over).

### Tool scoping

`NodeToolBinding.rule_binding_id` controls which rule sees a tool's result:

- **Set** → that tool's result is offered only to *that* rule's LLM prompt.
- **Unset** (shape-level) → offered to *every* rule attached to the same shape.

This matches the "tools picked while attaching rule X" semantic in
[`agent_tools/models.py:156-164`](../agent_tools/models.py#L156-L164).

---

## 4. Data model

All four tables live in the default `public` schema. FKs to
`agent_tools.NodeRuleBinding` / `NodeToolBinding` use `SET_NULL` so dropping
a binding never deletes audit history.

```
BatchExecutionRun (execution_batch_run)
  ├── id (uuid PK)
  ├── workflow (FK builder.Workflow PROTECT)
  ├── source_filename, claim_id_column
  ├── total_claims, completed, failed
  └── status: RUNNING | COMPLETED | PARTIAL | FAILED

RuleExecutionRun (execution_rule_run)
  ├── id (uuid PK)
  ├── batch (FK BatchExecutionRun SET_NULL, nullable for single-claim runs)
  ├── workflow (FK builder.Workflow PROTECT)
  ├── claim_id, claim_payload (JSON), raw_fetch (JSON)
  ├── final_decision_type, applied_codes (JSON), narrative
  └── status: RUNNING | COMPLETED | FAILED |
              TERMINATED_EARLY | TERMINATED_BY_PRECONDITION | FETCH_FAILED

RuleEvaluation (execution_rule_evaluation)
  ├── run (FK RuleExecutionRun CASCADE)
  ├── rule_binding (FK agent_tools.NodeRuleBinding SET_NULL)
  ├── rule_key, rule_source (PRECONDITION|DECISION)
  ├── condition, action, matched, confidence, reasoning
  ├── decision_type, codes (JSON), tool_results_used (JSON)
  └── llm_provider, llm_ms

ToolInvocationRecord (execution_tool_invocation)
  ├── run (FK RuleExecutionRun CASCADE)
  ├── tool_binding (FK agent_tools.NodeToolBinding SET_NULL, nullable for
  │                  fetch/parse calls made before bindings are loaded)
  ├── tool_name, phase (FETCH | PARSE | EVALUATE)
  ├── args (JSON), ok, result (JSON), error
  └── duration_ms, called_at
```

---

## 5. REST API

### 5.1 `POST /api/execute/workflows/<workflow_id>/run-batch/`

`multipart/form-data`:

| Field | Required | Default | Notes |
|---|---|---|---|
| `file` | yes | — | `.xlsx` upload |
| `claim_id_column` | no | `claim_id` | Case-insensitive; also accepts `subscriber_id`, `claimid`, etc. |
| `sheet_name` | no | first sheet | |

Response (truncated example):

```json
{
  "batch_id": "uuid",
  "status": "COMPLETED",
  "total_claims": 50,
  "completed": 48,
  "failed": 2,
  "results": [
    {
      "claim_id": "CLM-12345",
      "run_id": "uuid",
      "status": "COMPLETED",
      "final_decision_type": "DENY",
      "applied_codes": ["E51", "346"],
      "narrative": "Claim denied because timely filing limit exceeded...",
      "terminated_at_shape_id": "9b1a…",
      "evaluations": [
        {"rule_key": "pre:42:5:0", "shape_id": "8f2c…", "shape_label": "Eligibility",
         "matched": true, "decision_type": "ALLOW",
         "reasoning": "Member eligible on DOS per Linx."},
        {"rule_key": "step:42:3:2", "shape_id": "9b1a…", "shape_label": "Timely filing",
         "matched": true, "decision_type": "DENY",
         "reasoning": "DOS − received_date = 195d > 180d INN limit",
         "codes": ["E51", "346"]}
      ],
      "tool_invocations": [
        {"tool": "linx_claim_search", "phase": "FETCH", "ok": true, "ms": 142},
        {"tool": "check_diagnosis",   "phase": "EVALUATE", "ok": true, "ms": 88}
      ]
    },
    {
      "claim_id": "CLM-67890",
      "run_id": "uuid",
      "status": "FETCH_FAILED",
      "error_message": "linx_claim_search returned no records"
    }
  ]
}
```

Failure modes:

- `400` — missing file, non-`.xlsx`, claim-id column not found, workflow has
  no `NodeRuleBinding` rows.
- `404` — unknown workflow id (raised from inside the inner pipeline).
- Per-claim errors are captured inline; the batch as a whole only `FAILED`s
  if the workbook itself can't be parsed.

### 5.2 `GET /api/execute/batches/<batch_id>/`

Returns a saved batch with all child run summaries — useful for the SPA to
re-render a prior run.

### 5.3 `GET /api/execute/runs/<run_id>/`

Returns one claim's full audit trail (all evaluations + all tool
invocations).

---

## 6. Local setup

```bash
conda activate uhc-agentic-backend
pip install -e ./uhc-execution-engine
```

**Migrations are intentionally not yet generated** — pending teammate
collaboration on related schema changes. When ready:

```bash
python manage.py makemigrations execution_app
python manage.py migrate
```

### 6.1 Smoke checks (no DB writes)

```bash
DJANGO_SETTINGS_MODULE=sop_backend.settings python -c "
import django; django.setup()
from uhc_execution_engine import RuleEnginePipeline, BatchRunner
from uhc_execution_engine.graph import build_graph
print('nodes =', sorted(build_graph().nodes))
from agent_tools.registry import iter_tools
names = {t.name for t in iter_tools()}
print('linx_claim_search:', 'linx_claim_search' in names)
print('llm_parse_claim_with_ontology:', 'llm_parse_claim_with_ontology' in names)
"
python manage.py check execution_app
```

Expected: 7 inner nodes + `__start__`, both tools present, `check` reports
no issues.

### 6.2 End-to-end smoke test (post-migration)

1. Pick a workflow with `NodeRuleBinding` rows
   (`GET /api/builder/workflows/<id>/attachable/` confirms).
2. Prepare a small `.xlsx` with header `subscriber_id` and 2–3 ids drawn
   from the linx mock fixtures in [`agent_tools/mock/`](../agent_tools/mock/).
3. ```bash
   curl -F file=@claims.xlsx -F claim_id_column=subscriber_id \
        -X POST http://localhost:8000/api/execute/workflows/<id>/run-batch/
   ```
4. Verify:
   - `results` has one entry per row.
   - `BatchExecutionRun` row written (`status=COMPLETED`).
   - One `RuleExecutionRun` per claim with `final_decision_type` set.
   - `RuleEvaluation` rows = sum of preconditions + decisions evaluated
     across all claims.
   - `ToolInvocationRecord` includes `phase=FETCH` for every claim plus
     `phase=EVALUATE` for every shape-level tool.

---

## 7. Integration boundaries

- **Bindings are the source of truth.** Rules come from `NodeRuleBinding`,
  tools from `NodeToolBinding`. `Shape.properties.sop_rules` has already
  been migrated out by
  [`agent_tools/migrations/0003_migrate_shape_properties.py`](../agent_tools/migrations/0003_migrate_shape_properties.py).
- **Rule sequencing + selection comes from the SPA via
  [`builder/bindings_sync.py`](../builder/bindings_sync.py).** When the
  canvas is saved, `extract_bindings_from_properties` wipes the shape's
  bindings and re-creates one `NodeRuleBinding` per entry in
  `shape.properties.sop_rules`, stamping `ordering = idx` from the array
  position. Auditors **select** by including/omitting rules in that array
  and **sequence** by reordering it. Same flow for tools, including the
  tool→rule `rule_binding_id` FK. The engine just reads
  `NodeRuleBinding.ordering` ([`rule_loader.py:98`](../uhc-execution-engine/src/uhc_execution_engine/rule_loader.py#L98))
  — no separate selection API needed.
- **Tools are invoked in-process** via `agent_tools.registry.get_tool()`,
  not via HTTP-posting to `/api/agent-tools/<name>/invoke`. Keeps the engine
  usable from CLI / Celery later; we can swap to HTTP without touching the
  pipeline.
- **`linx_claim_search` is the fixed fetcher for now.** Its schema (see
  [`agent_tools/tools/schemas/linx.py`](../agent_tools/tools/schemas/linx.py))
  requires `subscriber_id`; the Excel column maps to that field. If
  `llm_parse_claim_with_ontology` is bound on the workflow, it runs once
  per claim in the outer layer to normalize the raw Linx payload.
- **Canvas topology (`ShapeConnection`) is not yet used at runtime.**
  Shapes are iterated in flat canvas order
  (`shape.workbench.order` → `shape.order`) and rules within a shape in
  `NodeRuleBinding.ordering`. Topology-aware execution (DENY/STOP edges,
  branching on rule outcome) is a planned follow-up.

---

## 8. Known follow-ups

- **`NodeRuleBinding.ordering` end-to-end test** — sequencing flows
  SPA → [`bindings_sync.py`](../builder/bindings_sync.py) → DB →
  [`rule_loader.py`](../uhc-execution-engine/src/uhc_execution_engine/rule_loader.py)
  → `execute_shapes`. Each piece is verified in isolation; a single
  end-to-end test that reorders rules in the SPA payload and asserts the
  engine evaluates them in the new order would lock the contract.
- **Topology-aware execution** — walk `ShapeConnection` so a DENY edge can
  route to a specific downstream Shape, and so non-linear canvases (loops,
  branches) execute meaningfully. Today we walk every Shape in flat canvas
  order regardless of how the auditor wired the edges.
- **Celery parallelization** — batches run sequentially today. A Celery
  group around `BatchRunner._run_one` is the obvious upgrade once per-batch
  durations get painful.
- **Configurable claim fetcher** — `linx_claim_search` is hard-wired in
  [`claim_fetcher.py`](../uhc-execution-engine/src/uhc_execution_engine/claim_fetcher.py).
  Let the workflow declare which fetch tool to use (e.g. via a convention
  on a designated "fetch" Shape), so non-Linx sources work too.
- **Per-claim parallelism inside a Shape** — rules on the same Shape are
  evaluated serially; they're independent LLM calls and could fan out.
  Useful only once shapes routinely carry 4+ rules.

### Done in this iteration

- ~~**Migrations for `execution_app`**~~ — `execution_app/0001_initial.py`
  generated and applied; the 4 tables now exist in Postgres.
- ~~**Delete deprecated `n04_preconditions.py` / `n05_decisions.py`**~~ —
  files removed, exports cleaned out of [`agents/__init__.py`](../uhc-execution-engine/src/uhc_execution_engine/agents/__init__.py),
  back-compat reads of `precondition_results` / `decision_results` purged
  from `n06_aggregate.py` and `n07_persist_respond.py`. The legacy
  `TERMINATED_BY_PRECONDITION` status choice was also dropped from
  `RuleExecutionRun.STATUS_CHOICES` since it was never emitted.
- ~~**`LLMCallLog` integration**~~ — `LLMCallLog.job` is now nullable and a
  new nullable `execution_run` FK points at `execution_app.RuleExecutionRun`
  ([`sop_ingestion/migrations/0011_llmcalllog_execution_run.py`](../sop_ingestion/migrations/0011_llmcalllog_execution_run.py)).
  The engine's `llm.py` carries the active `run_id` through a `ContextVar`
  ([`execution_run_context`](../uhc-execution-engine/src/uhc_execution_engine/llm.py))
  set by `RuleEnginePipeline.run`, and writes one `LLMCallLog` row per
  attempt with `execution_run` populated. `n01_validate` now pre-creates a
  `RUNNING` `RuleExecutionRun` row so the FK target exists before any LLM
  call fires.

---

## 9. File map

| Concern | File |
|---|---|
| Public API class | [`uhc-execution-engine/src/uhc_execution_engine/pipeline.py`](../uhc-execution-engine/src/uhc_execution_engine/pipeline.py) |
| Batch runner | [`uhc-execution-engine/src/uhc_execution_engine/batch.py`](../uhc-execution-engine/src/uhc_execution_engine/batch.py) |
| LangGraph wiring | [`uhc-execution-engine/src/uhc_execution_engine/graph.py`](../uhc-execution-engine/src/uhc_execution_engine/graph.py) |
| 6 nodes (per-Shape iteration) | [`uhc-execution-engine/src/uhc_execution_engine/agents/`](../uhc-execution-engine/src/uhc_execution_engine/agents/) |
| Per-Shape evaluator (v2) | [`uhc-execution-engine/src/uhc_execution_engine/agents/n_execute_shapes.py`](../uhc-execution-engine/src/uhc_execution_engine/agents/n_execute_shapes.py) |
| Rule sequencing / selection bridge (SPA → DB) | [`builder/bindings_sync.py`](../builder/bindings_sync.py) |
| LLM helper | [`uhc-execution-engine/src/uhc_execution_engine/llm.py`](../uhc-execution-engine/src/uhc_execution_engine/llm.py) |
| Rule + tool hydration | [`uhc-execution-engine/src/uhc_execution_engine/rule_loader.py`](../uhc-execution-engine/src/uhc_execution_engine/rule_loader.py) |
| Tool invocation | [`uhc-execution-engine/src/uhc_execution_engine/tool_runner.py`](../uhc-execution-engine/src/uhc_execution_engine/tool_runner.py) |
| Claim fetch + parse | [`uhc-execution-engine/src/uhc_execution_engine/claim_fetcher.py`](../uhc-execution-engine/src/uhc_execution_engine/claim_fetcher.py) |
| Excel parser | [`uhc-execution-engine/src/uhc_execution_engine/xlsx_parser.py`](../uhc-execution-engine/src/uhc_execution_engine/xlsx_parser.py) |
| Django models | [`execution_app/models.py`](../execution_app/models.py) |
| REST views | [`execution_app/views.py`](../execution_app/views.py) |
| URL routes | [`execution_app/urls.py`](../execution_app/urls.py) |
