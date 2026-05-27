# Data model

Every Django model in this repo, what it stores, and how it connects.

The schema spans four Django apps and is mirrored in two ways:

* **Postgres** holds canonical relational state for everything below.
* **Neo4j** holds the SOP knowledge graph (mirror of `AuditGraphNode` + `AuditGraphEdge`) for traversal queries.

Tables are listed app-by-app, ordered by their position in the dependency tree.
The four apps are `builder` (workflows + canvas), `sop_ingestion` (ingested SOPs
+ pipeline observability), `agent_tools` (tool registry + per-Shape rule/tool
bindings — replaces the legacy `Shape.properties.{sop_rules, tool_calls}` JSON
blobs), and `execution_app` (per-claim execution runs + audit trail).

---

## 1. Builder app — `/api/builder/`

Source: [builder/models.py](../builder/models.py).

### 1.1 Hierarchy

```
Workflow                                — top-level container
  └─ WorkArea                           — swim-lane / phase
        └─ Workbench                    — sub-canvas inside a phase
              └─ Shape                  — one xyflow node
                    └─ properties JSONB — sop_rules[] + tool_calls[]
ShapeConnection                         — directed edge (kept for completeness)
```

### 1.2 `Workflow` — `builder_workflow`

| Field         | Type            | Notes                                                                  |
|---------------|-----------------|------------------------------------------------------------------------|
| `id`          | UUID PK         | Auto-generated.                                                        |
| `name`        | CharField(255)  | Human label.                                                           |
| `slug`        | SlugField(255)  | Unique, auto-derived from `name` on create.                            |
| `description` | TextField       | Blank-default.                                                         |
| `is_active`   | bool            | Indexed.                                                               |
| `metadata`    | JSONB           | Free-form. Carries `runtime_agents[]` (with `endpoint_id`, never `auth_token`) and `sop_ingestion_jobs[]` (write-back from create). |
| `owner_id`    | Char(64)        | Bare UUID from the corebackend (no FK — auth lives in a separate service). |
| `owner_email` | EmailField      | Cached for display.                                                    |
| `created_at`  | datetime        |                                                                        |
| `updated_at`  | datetime        | `ordering = ["-updated_at"]`.                                          |

### 1.3 `WorkArea` — `builder_work_area`

A swim-lane / phase inside a workflow. UUID PK, FK to `Workflow`. Fields: `name`, `description`, `order`, `color`, `position_x/y`, `width`, `height`, `metadata` (JSONB).

Indexed on `(workflow, order)`.

### 1.4 `Workbench` — `builder_workbench`

A sub-canvas inside a `WorkArea`. UUID PK, FK to `WorkArea`. Fields:

* `name`, `description`
* `node_key` — stable per-workflow handle (lets edges reference freshly-created shapes by key, not UUID).
* `kind` — classification slug (`"Eligibility"`, `"Adjudication"`, …) used by the SPA for sub-palette filters.
* `config` (JSONB), `order`, `position_x/y`, `width`, `height`, `style` (JSONB).

Indexed on `(work_area, order)` and on `kind`.

### 1.5 `Shape` — `builder_shape`

One placed flow-chart node on the canvas. UUID PK, FK to `Workbench`, FK to `ShapeDefinition` (PROTECT — palette items aren't deleted).

| Field          | Type            | Notes                                                                |
|----------------|-----------------|----------------------------------------------------------------------|
| `definition`   | FK              | The palette item this shape was instantiated from.                  |
| `label`        | Char(255)       | Instance override.                                                  |
| `description`  | TextField       |                                                                      |
| `position_x/y` | Float           |                                                                      |
| `width/height` | Float           |                                                                      |
| `style`        | JSONB           | Instance style overrides.                                           |
| `properties`   | JSONB           | Values for the `property_schema` declared on the definition. **Carries `sop_rules[]` (rule keys) and `tool_calls[]` (endpoint keys).** |
| `order`        | uint            |                                                                      |

### 1.6 `ShapeConnection` — `builder_shape_connection`

Directed edge between two `Shape`s. Kept for completeness — the live frontend stores edges in `localStorage` and only persists them on canvas save via `PUT /workflows/<id>/graph/`.

Fields: `source_shape`, `target_shape`, `source_port`, `target_port`, `label`, `condition_label` (e.g. `"Yes"` / `"No"`), `waypoints` (JSONB), `style` (JSONB).

### 1.7 Catalog tables

These three tables drive the entire server-driven UI. The SPA never hardcodes shape, sidebar, or dashboard knowledge.

#### `ShapeCategory` — `builder_shape_category`

Palette section header. Fields: `slug`, `label`, `description`, `order`, `is_active`.

#### `ShapeDefinition` — `builder_shape_definition`

One draggable palette item. Fields:

| Field             | Notes                                                                |
|-------------------|----------------------------------------------------------------------|
| `category`        | FK to `ShapeCategory`.                                              |
| `slug`            | Unique.                                                              |
| `label`           |                                                                      |
| `kind`            | Renderer key — `rectangle` / `diamond` / `cylinder` / `hexagon` / etc. |
| `svg_path`        | Optional pre-computed SVG `d` attribute. When set the renderer needs no kind-specific code. |
| `viewbox`         | Default `"0 0 100 100"`.                                            |
| `default_label`, `default_width`, `default_height` | Starting values for fresh drops. |
| `default_style`   | JSONB — `{fill, stroke, text, accent, ...}`.                        |
| `ports`           | JSONB — array of `{id, x, y, side, kind: 'source'|'target'|'both'}` with x/y as 0..1 percentages. |
| `property_schema` | JSONB — `[{name, label, type, options?}]`. Drives the inspector form. |

The 8 canonical shapes (seeded by `seed_builder_catalog`):

| Slug                | Flowchart label    | Role                  |
|---------------------|--------------------|-----------------------|
| `round-rectangle`   | Terminator         | Start / End           |
| `rectangle`         | Process            | Action                |
| `diamond`           | Decision           | Branch / Conditional  |
| `parallelogram`     | Data               | Input / Output        |
| `hexagon`           | Preparation        | Setup / Init          |
| `cylinder`          | Database           | Persistent storage    |
| `circle`            | Connector          | Reference / Connector |
| `arrow-rectangle`   | Predefined Process | Subprocess            |

#### `NavItem` — `builder_nav_item`

One sidebar entry. Fields: `slug`, `label`, `icon` (lucide-react name), `href`, `section` (group header), `min_role` (`MEMBER` | `ADMIN`), `order`, `is_active`. Admin-only items are filtered out for `MEMBER` tokens.

#### `DashboardWidget` — `builder_dashboard_widget`

One dashboard tile. Fields: `slug`, `label`, `icon`, `kind` (`stat` | `chart` | `list` | `card`), `value` (for static stat tiles), `query` (REST path to populate dynamic tiles), `color_class`, `order`, `is_active`.

---

## 2. SOP ingestion app — `/api/ingest/`

Source: [sop_ingestion/models.py](../sop_ingestion/models.py).

### 2.1 Job-tracking tables

#### `IngestionJob` — `sop_ingestion_ingestionjob`

One row per ingestion run. UUID PK.

| Field             | Type             | Notes                                                                |
|-------------------|------------------|----------------------------------------------------------------------|
| `job_id`          | UUID PK          |                                                                      |
| `workflow`        | FK Workflow      | Nullable — old standalone runs aren't tied to a workflow.            |
| `seed_url`        | URL              | Starting point for the BFS crawl.                                    |
| `status`          | enum             | `QUEUED` / `RUNNING` / `COMPLETED` / `FAILED` / `PARTIAL`.            |
| `docs_queued`     | uint             | Set after pipeline finishes.                                         |
| `docs_processed`  | uint             |                                                                      |
| `docs_failed`     | uint             |                                                                      |
| `max_depth`       | uint8            | BFS limit.                                                           |
| `max_docs`        | uint             | Hard cap on documents.                                               |
| `llm_provider`    | str              | `"anthropic"` (default) or `"openai"`.                              |
| `llm_model`       | str              | Default `"claude-sonnet-4-5-20250929"`.                              |
| `celery_task_id`  | str              | Populated after dispatch.                                            |
| `created_at`, `started_at`, `completed_at` | datetimes |                                                              |
| `summary`         | JSONB            | `final_summary` from the pipeline.                                  |
| `errors`          | JSONB list       | Truncated at 200.                                                    |
| `total_llm_calls`, `total_tokens_in`, `total_tokens_out` | uints | Aggregated from `LLMCallLog` via `refresh_llm_totals()`. |

Helpers: `mark_started()`, `mark_done(summary, errors)`, `mark_failed(reason)`, `refresh_llm_totals()`.

#### `IngestedDocument` — `sop_ingestion_ingesteddocument`

One row per document inside a job. Unique on `(job, content_hash)`. Fields:

`url`, `content_hash` (sha256[:16]), `doc_format` (HTML / DOCX / XLSX / PDF), `depth`, `status`, `neo4j_sop_id`, `pg_sop_id`, `steps_count`, `rules_count`, `codes_count`, `links_found`, `created_at`.

#### `PipelineStageLog` — `sop_ingestion_pipelinestagelog`

One row per LangGraph node execution. Written **real-time** by [pg_logger.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/pg_logger.py) using a dedicated psycopg2 autocommit connection — visible from the HTML viewer while the job is still running.

Fields: `job`, `stage_name`, `doc_url`, `doc_format`, `doc_depth`, `started_at`, `completed_at`, `duration_ms`, `status` (`OK` / `ERROR` / `SKIP`), `error_detail`.

#### `LLMCallLog` — `sop_ingestion_llmcalllog`

One row per LLM API call across **both** the ingestion pipeline and the
execution engine. Each row sets exactly one of two FKs to identify its
owner; the other is `NULL`.

Fields:

* `job` (FK `IngestionJob`, **nullable**) — set for rows written by the SOP-ingestion pipeline.
* `execution_run` (FK `execution_app.RuleExecutionRun`, **nullable**) — set for rows written by the execution engine.
* `stage`, `agent_name`, `llm_provider`, `llm_model`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `duration_ms`, `success`, `error_message`, `called_at`.

### 2.2 Audit tables (the SOP itself)

These tables model an SOP the way a human auditor reads one:

1. Open `AuditSop` to understand what the policy is.
2. Verify `AuditPrecondition`s (platform, LOB, eligibility).
3. Walk the `AuditStep` decision tree.
4. At each step pick a matching `AuditDecision` row.
5. Consult `AuditCode` / `AuditGroupLimit` / `AuditAnnotation` / `AuditReference` as needed.

#### `AuditSop` — `sop_ingestion_auditsop`

Root SOP record. Unique on `(job, content_hash)`. Fields:

| Field             | Notes                                                                |
|-------------------|----------------------------------------------------------------------|
| `job`             | FK `IngestionJob`.                                                  |
| `url`, `content_hash`, `doc_format`, `neo4j_sop_id` | Identity.                       |
| `title`           | From metadata.                                                       |
| `purpose`         | LLM one-liner.                                                       |
| `llm_summary`     | 3–4 sentence executive summary.                                     |
| `narrative_context` | LLM story-style intro (5–8 sentences). Written by `sop_overview_narrator` in `a17_narrative`. |
| `platform`        | E.g. `"Facets"`.                                                    |
| `lob`             | JSONB list — `["Commercial", "Medicare Advantage"]`.                 |
| `audience`        | JSONB list — `["Claims Examiners"]`.                                |
| `state_div`, `product` | Strings.                                                        |
| `effective_date`, `revision_date` | Strings (the SOPs aren't dates-formatted consistently). |
| `crawl_depth`, `parent_url` | BFS context.                                              |
| `step_count`, `decision_count`, `code_count`, `precondition_count` | Counts updated after write. |
| `raw_text`        | Full plain-text dump.                                                |
| `parse_warnings`  | JSONB list.                                                          |
| `crawled_at`, `updated_at` | Timestamps.                                                |

#### `AuditPrecondition` — `sop_ingestion_auditprecondition`

Pre-decision-tree rules. Category enum: `PLATFORM` / `AUDIENCE` / `LOB` / `ELIGIBILITY` / `COVERAGE` / `GENERAL`.

Fields: `sop`, `display_order`, `category`, `label`, `content_text`, `llm_rules` (JSONB list of `{condition, action, decision_type, is_exception}`), `is_blocking`.

#### `AuditStep` — `sop_ingestion_auditstep`

One decision point. Unique on `(sop, step_number)`.

Fields: `sop`, `step_number`, `question`, `intro_text`, `is_terminal`, `terminal_action` (e.g. `"Process claim (F3)"`), `is_sub_procedure`, `sub_procedure_name`, `neo4j_node_id`, `narrative_context` (2–3 sentence LLM paragraph).

#### `AuditDecision` — `sop_ingestion_auditdecision`

One row in the If/Then table for a step. Decision-type enum: `DENY` / `ALLOW` / `BYPASS` / `PEND` / `REFER` / `SYSTEM` / `STOP` / `WAIVE` / `CONDITIONAL`.

Fields: `step`, `row_index`, `condition_if`, `condition_and`, `action_text`, `action_summary`, `action_line`, `action_claim`, `decision_type`, `goto_step` (nullable — branches to step N), `is_final` (terminates), `eob_codes`, `ex_codes`, `denial_codes`, `system_actions`, `all_codes` (merged for fast lookup), `neo4j_edge_id`.

#### `AuditGroupLimit` — `sop_ingestion_auditgrouplimit`

Group-specific timely-filing windows.

Fields: `sop`, `group_name`, `inn_days`, `oon_days`, `limit_days`, `limit_months`, `limit_years`, `calculation_basis` (`DOS` / `PAID_DATE` / `EOB_DATE`), `network_type` (`INN` / `OON` / `BOTH`), `member_submitted_only`, `exceptions` (JSONB list), `special_notes` (JSONB list), `raw_text`.

#### `AuditCode` — `sop_ingestion_auditcode`

Every claims code mentioned in the SOP. Unique on `(sop, code_value, code_type)`. Code-type enum: `EOB` / `EX` / `DENIAL` / `SYSTEM_ACT` / `POS` / `REVENUE` / `BILL_TYPE` / `MODIFIER` / `FREQUENCY` / `CPT` / `UNKNOWN`.

Fields: `sop`, `code_value`, `code_type`, `description`, `context_snippet`, `source_step` (nullable), `source_field`, `confidence` (0..1).

#### `AuditDateCondition` — `sop_ingestion_auditdatecondition`

Date-range applicability. Fields: `sop`, `date_from`, `date_to`, `effective_date`, `context_text`, `applies_to`.

#### `AuditAnnotation` — `sop_ingestion_auditannotation`

Embedded notes / alerts. Type enum: `NOTE` / `ALERT` / `EXCEPTION` / `TIP` / `WARNING` / `HIGHLIGHT`.

Fields: `sop`, `step` (nullable), `annotation_type`, `content_text`, `is_claim_impact`.

#### `AuditReference` — `sop_ingestion_auditreference`

Cross-references to other SOPs / calculators / policies. Fields: `sop`, `step` (nullable), `ref_text`, `ref_url`, `ref_type`, `is_resolved`.

### 2.3 Knowledge graph (materialised)

The same SOP re-expressed as `(node, edge)` pairs for graph traversal. **Single source of truth for the SPA's React-Flow viewer.** Neo4j is hydrated from these rows by the writer agents.

#### `AuditGraphNode` — `sop_ingestion_auditgraphnode`

Unique on `(sop, node_key)`. Indexed on `(sop, node_type)`.

Fields: `sop`, `node_key` (stable e.g. `"doc"`, `"step_3"`, `"dec_42"`), `node_type`, `label`, `details` (JSONB), `ref_table` + `ref_id` (back-ref to source row), `display_order`.

Node types: `DOCUMENT` / `META` / `PRE_SECTION` / `PRE_RULE` / `STEP` / `DECISION` / `ANNOTATION` / `GROUP_LIMIT` / `CODE` / `DATE_COND` / `REFERENCE`.

#### `AuditGraphEdge` — `sop_ingestion_auditgraphedge`

Indexed on `(sop, rel_type)` and `(source, rel_type)`.

Fields: `sop`, `source` (FK `AuditGraphNode`), `target` (FK `AuditGraphNode`), `rel_type`, `label`, `details` (JSONB).

Relationship types: `HAS_META` / `HAS_PRE_SECTION` / `HAS_RULE` / `HAS_STEP` / `HAS_DECISION` / `HAS_ANNOTATION` / `HAS_GROUP_LIMIT` / `HAS_CODE_REF` / `HAS_DATE_COND` / `REFERENCES` / `GOTO`.

---

## 3. Agent-tools app — `/api/agent-tools/`

Source: [agent_tools/models.py](../agent_tools/models.py).

This app owns the DB-backed LangChain tool registry and the relational
replacement for `Shape.properties.{sop_rules, tool_calls}`. All three tables
live in the dedicated Postgres schema `agent_tools` (created by migration
`0000_create_schema`) — Django's `search_path` is set to
`public,agent_tools` in [`sop_backend/settings.py`](../sop_backend/settings.py)
so FKs resolve across schemas.

#### `Tool` — `agent_tools.tool`

One row per LangChain `StructuredTool` (and optionally per registered runtime
HTTP agent). Seeded idempotently from
[`agent_tools/registry.py`](../agent_tools/registry.py) at app start.

Fields: `id`, `name` (unique slug), `display_name`, `description`,
`kind` (`langchain` | `api_agent`), `invoke_url`, `args_schema` (JSONB —
Pydantic JSON Schema for the form renderer), `endpoint_id` (for `api_agent`
rows — links back to `uhc-api-agent`'s endpoint registry),
`metadata` (JSONB), `is_active`.

#### `NodeRuleBinding` — `agent_tools.node_rule_binding`

One SOP rule attached to one canvas Shape. Replaces an entry in the legacy
`Shape.properties.sop_rules` array. Wipe-and-reinsert per Shape on every
canvas save (driven by [`builder/bindings_sync.py`](../builder/bindings_sync.py)).

Unique on `(shape, rule_key)`. Indexed on `(shape, ordering)` and
`(sop, rule_key)`.

Fields:

* `shape` — FK `builder.Shape`.
* `sop` — FK `sop_ingestion.AuditSop`.
* `rule_key` — opaque string produced by `builder.views.attachable`, e.g. `pre:<sop_id>:<precondition_id>:<idx>` or `step:<sop_id>:<step_number>:<row_index>`.
* `condition`, `action` — auditor-editable overrides (start as copies of the SOP's authoritative text; override wins when non-empty).
* `references_json`, `excluded_by_json` — JSONB lists of related rule keys, snapshotted at attach time.
* `html_reference_json` — JSONB snapshot of the source HTML reference the SPA renders next to the rule.
* `ordering` — integer, **the auditor's selected order on this shape**. Written by `bindings_sync` from the array index in the SPA payload; read by the execution engine via `rule_loader.py`.

#### `NodeToolBinding` — `agent_tools.node_tool_binding`

One tool call attached to one canvas Shape. Replaces an entry in the legacy
`Shape.properties.tool_calls` array.

Unique on `(shape, tool, rule_binding)`. Indexed on `(shape, ordering)`,
`tool`, `rule_binding`.

Fields:

* `shape` — FK `builder.Shape`.
* `tool` — FK `Tool` (PROTECT).
* `args_template` — JSONB defaults pre-filled in the SPA invoke form.
* `rule_binding` — **nullable** FK `NodeRuleBinding` (SET_NULL). When set, this is the "tools picked while attaching that rule" relationship — the execution engine scopes that tool's result to **only** that rule's LLM prompt. When NULL, the tool is shape-level and offered to **every** rule on the shape.
* `ordering` — integer; auditor's selected order on this shape.

---

## 4. Execution-engine app — `/api/execute/`

Source: [execution_app/models.py](../execution_app/models.py).

Persists batch execution runs, per-claim runs, per-rule evaluations, and
per-tool invocations. All four tables live in the default `public` schema.
FKs to `agent_tools` tables use `SET_NULL` so dropping a binding never
deletes audit history.

#### `BatchExecutionRun` — `execution_batch_run`

One row per uploaded `.xlsx`.

Fields: `id` (UUID PK), `workflow` (FK `builder.Workflow` PROTECT),
`source_filename`, `claim_id_column`, `total_claims`, `completed`, `failed`,
`started_at`, `finished_at`, `status` (`RUNNING` / `COMPLETED` / `PARTIAL` / `FAILED`),
`error_message`.

#### `RuleExecutionRun` — `execution_rule_run`

One row per claim. Pre-created in status `RUNNING` by `n01_validate` so that
`LLMCallLog` rows written during evaluation have a valid FK target;
finalised in `n07_persist_respond` via `update_or_create` once the verdict is
known.

Indexed on `(batch, claim_id)`.

Fields:

* `id` (UUID PK), `batch` (FK `BatchExecutionRun` SET_NULL — nullable for single-claim runs), `workflow` (FK `builder.Workflow` PROTECT).
* `claim_id` (the value pulled from the Excel column), `claim_payload` (JSONB — parsed claim handed to the engine), `raw_fetch` (JSONB — raw `linx_claim_search` response).
* `started_at`, `finished_at`.
* `status` — `RUNNING` / `COMPLETED` / `FAILED` / `TERMINATED_EARLY` / `FETCH_FAILED`.
* `final_decision_type` — e.g. `DENY` / `ALLOW` / `PEND` / ...
* `applied_codes` (JSONB list), `narrative`, `error_message`.

Also reverse-related to `sop_ingestion.LLMCallLog` via the nullable
`execution_run` FK (one row per LLM attempt during this run).

#### `RuleEvaluation` — `execution_rule_evaluation`

One row per rule evaluated for the run. Bulk-inserted in
`n07_persist_respond`; ordered by `(shape canvas order, rule order)`.

Fields:

* `run` — FK `RuleExecutionRun` CASCADE.
* `order_index` — global order across all Shapes for this run.
* `rule_binding` — FK `agent_tools.NodeRuleBinding` SET_NULL.
* `rule_key`, `rule_source` (`PRECONDITION` | `DECISION`).
* `condition`, `action` — captured at evaluation time (so the audit trail survives later edits to the binding).
* `matched`, `confidence` (float), `reasoning`.
* `decision_type`, `codes` (JSONB), `tool_results_used` (JSONB list of binding ids).
* `llm_provider`, `llm_ms` — the provider/duration of the retained attempt; granular per-attempt cost lives in `LLMCallLog`.

#### `ToolInvocationRecord` — `execution_tool_invocation`

One row per tool call. Includes outer-layer fetch/parse calls (the
`BatchRunner` writes these from `batch.py`) and inner-pipeline tool calls
(the `run_tools` node writes these).

Fields:

* `run` — FK `RuleExecutionRun` CASCADE.
* `tool_binding` — FK `agent_tools.NodeToolBinding` SET_NULL (NULL for fetch/parse calls that happen before bindings are loaded).
* `tool_name`, `phase` (`FETCH` | `PARSE` | `EVALUATE`).
* `args` (JSONB), `ok`, `result` (JSONB), `error`, `duration_ms`, `called_at`.

---

## 5. Foreign-key map

```
Workflow ◄── ingestion_jobs ── IngestionJob ◄── audit_sops ── AuditSop ◄── preconditions ── AuditPrecondition
   ▲                                ▲                          │                              ▲
   │                                │                          ├── steps ── AuditStep ◄── decisions ── AuditDecision
   │                                │                          │              │
   │                                │                          │              └── annotations / references
   │                                │                          │
   │                                │                          ├── codes / group_limits / date_conditions
   │                                │                          ├── annotations / references
   │                                │                          ├── graph_nodes ── AuditGraphNode ◄── edges_out/in ── AuditGraphEdge
   │                                │                          │
   │                                ├── documents ── IngestedDocument
   │                                ├── stage_logs ── PipelineStageLog
   │                                └── llm_calls ── LLMCallLog ◄────────────────────────────────────────┐
   │                                                                                                      │ (nullable)
   ├── execution_batches ── BatchExecutionRun ◄── runs ── RuleExecutionRun ─────────────────────────────────┤
   │                                                       │                                              │
   │                                                       ├── evaluations ── RuleEvaluation ──► NodeRuleBinding (SET_NULL)
   │                                                       └── tool_invocations ── ToolInvocationRecord ─► NodeToolBinding (SET_NULL)
   │
   └── work_areas ── WorkArea ── workbenches ── Workbench ── shapes ── Shape ── (FK ShapeDefinition)
                                                                          │
                                                                          ├── rule_bindings ── NodeRuleBinding ──► AuditSop
                                                                          ├── tool_bindings ── NodeToolBinding ──► Tool
                                                                          │                                                ▲
                                                                          │                                  rule_binding (nullable FK
                                                                          │                                  to NodeRuleBinding)
                                                                          │
                                                                  ShapeConnection (source/target FK Shape)
```

`LLMCallLog` sets **exactly one** of `job` (FK `IngestionJob`) or
`execution_run` (FK `RuleExecutionRun`); the other is NULL. Ingestion-pipeline
rows use the former, execution-engine rows the latter.

---

## 6. JSONB shapes (cheat sheet)

A few JSONB columns carry meaningful structure worth knowing.

### `Workflow.metadata`

```json
{
  "runtime_agents": [
    {
      "name": "Eligibility check",
      "url": "https://api.example.com/eligibility",
      "method": "POST",
      "auth_type": "bearer",
      "description": "Verify member coverage",
      "endpoint_id": "ep_a1b2c3"
    }
  ],
  "sop_ingestion_jobs": [
    { "job_id": "...", "seed_url": "...", "status": "RUNNING" }
  ],
  "attachment_errors": []
}
```

### `Shape.properties`

The SPA still sends `sop_rules` + `tool_calls` arrays as part of the canvas
save, but they are **not** the source of truth at runtime —
[`builder/bindings_sync.py`](../builder/bindings_sync.py) projects them into
the relational `NodeRuleBinding` / `NodeToolBinding` tables on every PUT.
Array index becomes `ordering` on the corresponding row.

```json
{
  "label": "Verify timely filing",
  "sop_rules": [
    {
      "key": "pre:22:3:0",
      "sop_id": 22,
      "condition": "Member is Commercial LOB",
      "action": "Apply 90-day timely filing",
      "references": [],
      "excluded_by": [],
      "html_reference": { "url": "...", "anchor": "...", "raw_text": "..." }
    },
    {
      "key": "step:22:4:1",
      "sop_id": 22,
      "condition": "DOS > 180 days from received date",
      "action": "Deny — exceeded timely filing",
      "references": ["step:22:4:0"],
      "excluded_by": []
    }
  ],
  "tool_calls": [
    {
      "tool_id": "uuid",
      "name": "linx_claim_search",
      "args_template": { "subscriber_id": "" },
      "rule_key": "step:22:4:1"
    }
  ]
}
```

On GET, [`hydrate_properties_with_bindings`](../builder/bindings_sync.py) in
`builder/serializers.py` reads back from the binding tables (ordered by
`ordering`) and re-attaches SOP-side metadata, so the SPA sees a consistent
envelope.

### `AuditPrecondition.llm_rules`

```json
[
  { "condition": "Member is Commercial LOB", "action": "Apply 90-day timely filing", "decision_type": "CONDITIONAL", "is_exception": false }
]
```

### `AuditDecision.all_codes`

Merged superset of `eob_codes + ex_codes + denial_codes + system_actions` for fast `JSONB ? code` queries.

```json
["E51", "003", "346", "F4"]
```
