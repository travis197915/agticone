# Storage Layout — Postgres, Neo4j, Redis (and Mongo)

A single SOP ingestion fans out into **four** stores. Each store has a clear
job: structured audit (PG), graph traversal (Neo4j), short-lived agent
context + caches (Redis), and raw artefact archive (Mongo).

This document is the source-of-truth for *what lives where* and *how the
stores cross-reference each other*.

---

## 0. Cross-store identity

Every ingested SOP carries the same identity across all stores:

| Identifier                 | Postgres column                             | Neo4j property                              | Notes                                            |
|----------------------------|---------------------------------------------|---------------------------------------------|--------------------------------------------------|
| **SOP id**                 | `sop_ingestion_auditsop.neo4j_sop_id`       | `n.sop_id` on every graph node              | Example: `OBH_Facets_Duplicate_Claim_Handling:6b027ecc0b3c7439` |
| **Job id (UUID)**          | `sop_ingestion_ingestionjob.job_id`         | n/a                                         | Used in Redis blackboard keys                    |
| **Graph node id**          | `sop_ingestion_auditgraphnode.node_key`     | `n.node_key`                                | Stable string, e.g. `step_7`, `pre_4_r2`         |
| **Graph node type**        | `sop_ingestion_auditgraphnode.node_type`    | `n.node_type` + Neo4j label                 | One of the canonical NODE_TYPES                  |
| **Graph relationship**     | `sop_ingestion_auditgraphedge.rel_type`     | `type(r)`                                   | One of the canonical EDGE_TYPES                  |

That means: pick any node in PG, copy its `(neo4j_sop_id, node_key)` pair,
and you can locate the exact same node in Neo4j (or vice versa).

---

## 1. PostgreSQL — primary system of record

Connection: configured via `cfg.pg_host / pg_port / pg_user / pg_password / pg_database`.
Django manages the schema; migrations live under `sop_ingestion/migrations/`.

### 1.1 Job & observability tables

| Table                                       | Purpose                                                                                                             |
|---------------------------------------------|---------------------------------------------------------------------------------------------------------------------|
| `sop_ingestion_ingestionjob`                | One row per ingestion request. Tracks status, totals, LLM provider/model, timing, summary JSON, errors.             |
| `sop_ingestion_ingesteddocument`            | One row per fetched document inside a job. Captures URL, content-hash, depth, counts of steps/rules/codes.          |
| `sop_ingestion_pipelinestagelog`            | Every LangGraph stage execution (intake, fetch, parse, enrich, context, validate, graph_synthesis, write_*, …).      |
| `sop_ingestion_llmcalllog`                  | Every LLM call from **both** the ingestion pipeline and the execution engine. Sets exactly one of `job` (FK `IngestionJob`) or `execution_run` (FK `execution_rule_run`) — see §1.5. Captures agent name, provider, model, prompt/completion tokens, latency_ms, success flag, error_message. |

### 1.2 Audit tables (the "relational view" of a SOP)

The shape mirrors how a human claims auditor would slice the SOP.

| Table                                       | One row per…                                                          | Key columns                                                                                                       |
|---------------------------------------------|-----------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------|
| `sop_ingestion_auditsop`                    | Ingested SOP document.                                                | `title`, `purpose`, `llm_summary`, `platform`, `lob[]`, `audience[]`, `effective_date`, `revision_date`, `neo4j_sop_id`, counts. |
| `sop_ingestion_auditprecondition`           | Pre-step section (overview, exceptions, eligibility notes, …).        | `category`, `label`, `content_text`, `llm_rules` (LLM-extracted atomic rules), `is_blocking`.                     |
| `sop_ingestion_auditstep`                   | Numbered audit step (1, 2, … plus a synthetic Step 0 for exceptions). | `step_number`, `question`, `intro_text`, `is_terminal`, `terminal_action`, `is_sub_procedure`.                    |
| `sop_ingestion_auditdecision`               | One "If / Then" row inside a step.                                    | `row_index`, `condition_if`, `condition_and`, `action_text`, `decision_type`, `goto_step`, `eob_codes`, `ex_codes`, `denial_codes`, `system_actions`, `all_codes`. |
| `sop_ingestion_auditgrouplimit`             | Group-specific limit (TFL & similar).                                 | `group_name`, `inn_days`, `oon_days`, `limit_days/months/years`, `calculation_basis`, `network_type`.             |
| `sop_ingestion_auditcode`                   | Healthcare code referenced anywhere in the SOP.                       | `code_value`, `code_type` (EOB/EX/POS/REV/CPT/…), `description`, `context_snippet`, `source_step`, `confidence`.  |
| `sop_ingestion_auditdatecondition`          | Date-bound rule (from/to/effective).                                  | `date_from`, `date_to`, `effective_date`, `context_text`, `applies_to`.                                           |
| `sop_ingestion_auditannotation`             | Inline annotation (note, warning, alert, tip).                        | `annotation_type`, `content_text`, `is_claim_impact`, optional `step` FK.                                         |
| `sop_ingestion_auditreference`              | Cross-reference to another document.                                  | `ref_text`, `ref_url`, `ref_type`, `is_resolved`, optional `step` FK.                                             |

### 1.3 Knowledge-graph tables (the "graph view" of the same SOP)

This is the **materialised graph** built by the agentic synthesis stage —
identical in structure to what is mirrored into Neo4j.

| Table                                       | Description                                                                                                                                          |
|---------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------|
| `sop_ingestion_auditgraphnode`              | One row per knowledge-graph node. Columns: `id`, `sop_id`, **`node_key`**, **`node_type`**, `label`, `details (jsonb)`, `ref_table`, `ref_id`, `display_order`. |
| `sop_ingestion_auditgraphedge`              | One row per knowledge-graph edge. Columns: `id`, `sop_id`, **`source_id`** (FK → `auditgraphnode`), **`target_id`** (FK → `auditgraphnode`), **`rel_type`**, `label`, `details (jsonb)`. |

**Canonical node types** (column `node_type`)
`DOCUMENT`, `META`, `PRE_SECTION`, `PRE_RULE`, `STEP`, `DECISION`,
`ANNOTATION`, `GROUP_LIMIT`, `CODE`, `DATE_COND`, `REFERENCE`.

**Canonical relationship types** (column `rel_type`)
Structural — `HAS_META`, `HAS_PRE_SECTION`, `HAS_RULE`, `HAS_STEP`,
`HAS_DECISION`, `HAS_ANNOTATION`, `HAS_GROUP_LIMIT`, `HAS_CODE_REF`,
`HAS_DATE_COND`, `REFERENCES`, `GOTO`.
Semantic (LLM-inferred) — `USES_CODE`, `OVERRIDES`, `IMPLIES`, `CITED_BY`,
`GUARDS`, `APPLIES_TO`.

`auditgraphedge.source_id` and `auditgraphedge.target_id` form **the
traversal table** — equivalent to Neo4j relationships. All graph
queries (parents, children, ancestors, descendants, semantic neighbours)
go through this one table.

`ref_table` / `ref_id` are reserved for back-pointers from a graph node
to its source row in section 1.2 (e.g. `STEP step_3` → `auditstep.id`).
Currently unset by the agentic writer; populating these is a planned
enhancement.

### 1.4 Agent-tools tables (`agent_tools` Postgres schema)

These three tables live in a dedicated Postgres schema named `agent_tools`
(created by migration `0000_create_schema`). Django's connection
`search_path` is `public,agent_tools`, so unqualified references resolve
across schemas.

| Table                                | One row per…                                                                              | Key columns                                                                                                       |
|--------------------------------------|-------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------|
| `agent_tools.tool`                   | Registered tool (LangChain `StructuredTool` or runtime HTTP agent).                       | `name`, `display_name`, `kind` (`langchain`/`api_agent`), `invoke_url`, `args_schema (jsonb)`, `endpoint_id`, `is_active`. |
| `agent_tools.node_rule_binding`      | SOP rule attached to one canvas Shape. Replaces an entry of legacy `Shape.properties.sop_rules`. | `shape_id` (FK `builder_shape`), `sop_id` (FK `sop_ingestion_auditsop`), `rule_key`, `condition`, `action`, `references_json`, `excluded_by_json`, `html_reference_json`, **`ordering`** (auditor's chosen sequence). |
| `agent_tools.node_tool_binding`      | Tool call attached to one canvas Shape. Replaces an entry of legacy `Shape.properties.tool_calls`. | `shape_id` (FK `builder_shape`), `tool_id` (FK `agent_tools.tool`), `args_template (jsonb)`, `rule_binding_id` (nullable FK back to `node_rule_binding`), **`ordering`**. |

`ordering` is the field that carries the auditor's sequencing choice from
the SPA into runtime: array index in the SPA payload → `ordering` integer
written by [`builder/bindings_sync.py`](../builder/bindings_sync.py) → read
back by the execution engine's
[`rule_loader.py`](../uhc-execution-engine/src/uhc_execution_engine/rule_loader.py).

### 1.5 Execution-engine tables (`public` schema)

Persistence for batch claim adjudication. All tables live in `public`.
FKs to `agent_tools.*` use `ON DELETE SET NULL` so dropping a binding
never deletes audit history.

| Table                            | One row per…                                                                  | Key columns                                                                                                       |
|----------------------------------|-------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------|
| `execution_batch_run`            | Uploaded `.xlsx`.                                                             | `workflow_id` (FK `builder_workflow`), `source_filename`, `claim_id_column`, `total_claims`, `completed`, `failed`, `status` (`RUNNING`/`COMPLETED`/`PARTIAL`/`FAILED`), `started_at`, `finished_at`. |
| `execution_rule_run`             | Claim. Pre-created in `RUNNING` by `n01_validate`; finalised by `n07`.        | `batch_id` (FK `execution_batch_run`, nullable), `workflow_id`, `claim_id`, `claim_payload (jsonb)`, `raw_fetch (jsonb)`, `status` (`RUNNING`/`COMPLETED`/`FAILED`/`TERMINATED_EARLY`/`FETCH_FAILED`), `final_decision_type`, `applied_codes (jsonb)`, `narrative`. |
| `execution_rule_evaluation`      | Rule evaluated for a run. Ordered by `(shape canvas order, rule order)`.      | `run_id`, `order_index`, `rule_binding_id` (nullable FK `agent_tools.node_rule_binding`), `rule_key`, `rule_source` (`PRECONDITION`/`DECISION`), `condition`, `action`, `matched`, `confidence`, `reasoning`, `decision_type`, `codes (jsonb)`, `tool_results_used (jsonb)`, `llm_provider`, `llm_ms`. |
| `execution_tool_invocation`      | Tool call (outer-layer FETCH/PARSE + inner-pipeline EVALUATE).                | `run_id`, `tool_binding_id` (nullable FK `agent_tools.node_tool_binding`), `tool_name`, `phase` (`FETCH`/`PARSE`/`EVALUATE`), `args (jsonb)`, `ok`, `result (jsonb)`, `error`, `duration_ms`, `called_at`. |

`sop_ingestion_llmcalllog.execution_run_id` points back at
`execution_rule_run.id` for engine-emitted rows. Aggregate token / latency
queries are straightforward:

```sql
SELECT llm_provider, llm_model,
       COUNT(*)                       AS calls,
       SUM(prompt_tokens + completion_tokens) AS tokens,
       SUM(duration_ms)              AS total_ms
FROM   sop_ingestion_llmcalllog
WHERE  execution_run_id = '<run_id>'
GROUP  BY llm_provider, llm_model;
```

### 1.6 Quick traversal recipes (Postgres-side equivalent of Cypher)

```sql
-- Parents of a node (one hop, like Neo4j  MATCH (p)-[]->(n {node_key:$k}) )
SELECT src.node_key, src.node_type, e.rel_type
FROM   sop_ingestion_auditgraphedge e
JOIN   sop_ingestion_auditgraphnode src ON src.id = e.source_id
JOIN   sop_ingestion_auditgraphnode tgt ON tgt.id = e.target_id
WHERE  tgt.node_key = 'pre_7_r1';

-- Full ancestor chain (recursive CTE — like  MATCH path = (anc)-[*1..]->(n) )
WITH RECURSIVE ancestors AS (
    SELECT id, node_key, node_type, 0 AS depth, ARRAY[node_key] AS path
    FROM   sop_ingestion_auditgraphnode WHERE node_key = 'pre_7_r1'
  UNION ALL
    SELECT n.id, n.node_key, n.node_type, a.depth + 1, a.path || n.node_key
    FROM   ancestors a
    JOIN   sop_ingestion_auditgraphedge e ON e.target_id = a.id
    JOIN   sop_ingestion_auditgraphnode n ON n.id = e.source_id
    WHERE  NOT n.node_key = ANY(a.path) AND a.depth < 10
)
SELECT depth, node_type, node_key FROM ancestors ORDER BY depth;
```

---

## 2. Neo4j — graph traversal

Connection: `cfg.neo4j_uri / neo4j_user / neo4j_password / neo4j_database`.

The graph in Neo4j is a **mirror** of `auditgraphnode` + `auditgraphedge`,
but rendered with native graph primitives so you can use Cypher and
the Neo4j Browser for path-finding and visualisation.

### 2.1 Labels

Every materialised graph node is written with **two** labels:

* the canonical type label (`DOCUMENT`, `STEP`, `DECISION`, `PRE_RULE`, …)
* a marker label `:GraphNode` so we can wipe the whole graph for a
  single SOP without touching anything else: `MATCH (n:GraphNode {sop_id:$s}) DETACH DELETE n`

### 2.2 Properties on each node

| Property            | Description                                                                                                  |
|---------------------|--------------------------------------------------------------------------------------------------------------|
| `sop_id`            | Same value as `auditsop.neo4j_sop_id` — the cross-store SOP identifier.                                       |
| `node_key`          | Same value as `auditgraphnode.node_key`. Together with `sop_id` this is the unique node identity.            |
| `node_type`         | Same value as `auditgraphnode.node_type`.                                                                    |
| `label`             | Same value as `auditgraphnode.label`.                                                                        |
| `display_order`     | For deterministic ordering of siblings.                                                                      |
| `is_graph_node`     | `true` — used as a wipe marker.                                                                              |
| `updated_at`        | Timestamp set on the last MERGE.                                                                             |
| **flattened details** | Every key in `auditgraphnode.details (jsonb)` is flattened into its own property. Example: `details.action="…"` becomes node prop `details_action`. Nested dicts are stringified to keep Neo4j happy. |

### 2.3 Relationships

Same `rel_type` values as the Postgres `auditgraphedge.rel_type` column.
Each edge carries:

* `label` — short human-readable rationale (e.g. the LLM's reasoning for an `OVERRIDES`).
* `details` — JSON-serialised string of any extra metadata.

### 2.4 Common Cypher

```cypher
// Whole SOP knowledge graph
MATCH (n:GraphNode {sop_id:$sop_id})
OPTIONAL MATCH (n)-[r]->(m)
RETURN n, r, m;

// Ancestors of a rule (parent chain)
MATCH path = (anc)-[*1..6]->(:GraphNode {sop_id:$sop_id, node_key:'pre_7_r1'})
RETURN [n IN nodes(path) | n.node_key] AS chain;

// What does this pre-step exception override?
MATCH (r:PRE_RULE {sop_id:$sop_id, node_key:'pre_7_r1'})-[o:OVERRIDES]->(t)
RETURN t.node_type, t.node_key, t.label, o.label AS rationale;

// All semantic edges in a SOP
MATCH (a)-[r]->(b)
WHERE a.sop_id = $sop_id
  AND type(r) IN ['OVERRIDES','IMPLIES','GUARDS','CITED_BY','APPLIES_TO','USES_CODE']
RETURN a.node_key, type(r), b.node_key, r.label;
```

---

## 3. Redis — agent blackboard, queues, caches

Connection: `cfg.redis_host / redis_port / redis_user / redis_password`,
all values decoded as strings.

Redis is a working memory, not a system of record — it can be flushed at
any time and the next ingest rebuilds whatever it needs.

### 3.1 Agentic graph-synthesis blackboard (`a16_graph_synthesis.py`)

Each agent in the graph-synthesis flow writes its partial output back to
Redis so downstream agents (and the assembler) can read it. TTL: **24 h**.

```
sop:graph:{job_id}:document        — DOCUMENT + META nodes  (GPT-4o)
sop:graph:{job_id}:pre_sections    — PRE_SECTION + PRE_RULE nodes  (Claude)
sop:graph:{job_id}:steps           — STEP + per-step ANNOTATION nodes  (GPT-4o)
sop:graph:{job_id}:decisions       — DECISION nodes + HAS_DECISION + GOTO  (Claude, batched)
sop:graph:{job_id}:codes           — CODE nodes + USES_CODE  (GPT-4o)
sop:graph:{job_id}:references      — REFERENCE/CODEREF/GROUP_LIMIT/DATE_COND/ANNOTATION  (GPT-4o)
sop:graph:{job_id}:semantic_edges  — OVERRIDES/IMPLIES/GUARDS/CITED_BY/APPLIES_TO  (Claude)
sop:graph:{job_id}:audit_trail     — LIST of  {"agent","status","detail"}  per agent
```

Inspect / debug commands:

```bash
redis-cli SCAN 0 MATCH 'sop:graph:*'
redis-cli LRANGE sop:graph:<job_id>:audit_trail 0 -1
redis-cli GET    sop:graph:<job_id>:decisions
```

### 3.2 Pipeline runtime caches (`a13_write_redis.py`)

| Key prefix                          | Purpose                                                                          |
|-------------------------------------|----------------------------------------------------------------------------------|
| `sop:cache:<content_hash>`          | Pre-parsed payload — dedup hot files on re-ingest.                               |
| `sop:queue:<job_id>`                | BFS frontier queue for the link discoverer.                                       |
| `sop:progress:<job_id>`             | Live progress counters (docs_processed / docs_queued).                            |
| `sop:job:<job_id>`                  | Tracking entry written by `redis_job_tracker` at intake.                          |

### 3.3 Other Redis usage

`get_redis(cfg)` returns a single shared client. The Anthropic and OpenAI
helpers do **not** use Redis directly; rate-limiter buckets (if added in
future) would live under `sop:ratelimit:…`.

---

## 4. MongoDB — raw archive (out-of-scope of the question, listed for completeness)

| Collection             | Stored                                                              |
|------------------------|---------------------------------------------------------------------|
| `sop_raw_documents`    | The raw bytes / source HTML / DOCX / XLSX / PDF (`mongo_raw_writer`). |
| `sop_parsed_documents` | The full parsed `PipelineState` per document (`mongo_parsed_writer`). |
| `sop_job_progress`     | Time-series progress events (`mongo_job_progress`).                 |

These collections are append-only and not relied on for queries — they
exist for replay, debugging, and re-running the pipeline against an
exact past artefact without re-fetching.

---

## 5. Which store should I query?

| You need to…                                              | Use                                                                       |
|-----------------------------------------------------------|---------------------------------------------------------------------------|
| List all SOPs / steps / decisions for reports & dashboards | **Postgres** audit tables (`auditsop`, `auditstep`, `auditdecision`, …).  |
| Find a rule by its text / code / category                  | **Postgres** — fast full-text on the audit tables and JSONB columns.      |
| See the whole knowledge graph, ancestors, paths            | **Postgres graph tables** (`auditgraphnode/edge`) or **Neo4j** (Cypher).  |
| Render graphs visually / do shortest-path traversal        | **Neo4j**. (Postgres recursive CTEs work too but Neo4j is faster.)        |
| List rules + tools attached to a Shape (with ordering)     | **Postgres** `agent_tools.node_rule_binding` / `node_tool_binding`.       |
| See what decision a workflow gave a claim, with reasoning  | **Postgres** `execution_rule_run` + `execution_rule_evaluation`.          |
| Re-render a prior batch upload in the SPA                  | **Postgres** `execution_batch_run` (parent) + its `runs`.                 |
| Inspect what each LLM agent produced during a run          | **Redis** under `sop:graph:{job_id}:*` (24-h TTL).                        |
| Replay an old ingest exactly                                | **MongoDB** — raw bytes + parsed PipelineState are archived.              |
| Audit every LLM call (provider, model, tokens, cost)       | **Postgres** `sop_ingestion_llmcalllog` (filter by `job_id` or `execution_run_id`). |
| See per-stage timing and errors (ingestion)                | **Postgres** `sop_ingestion_pipelinestagelog`.                            |
| See per-node timing and errors (execution)                 | The `stages` array on the per-claim response, or query `execution_tool_invocation` for tool calls. |

---

## 6. Data lifecycle

| Event                         | Postgres                                       | Neo4j                                          | Redis                                            | Mongo                          |
|-------------------------------|-----------------------------------------------|------------------------------------------------|--------------------------------------------------|--------------------------------|
| New ingest (job_id created)   | `IngestionJob` row created                    | —                                              | `sop:job:<job_id>` set                            | —                              |
| Each agent finishes           | `LLMCallLog` + `PipelineStageLog` row         | —                                              | `sop:graph:{job_id}:<section>` set               | —                              |
| `graph_synthesis_stage` done  | —                                              | —                                              | All 7 section keys + audit_trail populated        | —                              |
| `write_postgres` done         | `Audit*` + `AuditGraphNode/Edge` rows         | —                                              | —                                                | —                              |
| `write_neo4j` done            | —                                              | All `:GraphNode` nodes + edges merged          | —                                                | —                              |
| `write_mongo` done            | —                                              | —                                              | —                                                | Raw + parsed docs              |
| Re-ingest same SOP            | Graph tables wiped + reinserted for that SOP  | `:GraphNode {sop_id:$s}` wiped + reinserted    | New blackboard keys under new `job_id`           | New revisions inserted         |
| Manual wipe                   | `python manage.py shell ... .all().delete()`  | `MATCH (n) DETACH DELETE n`                    | `redis-cli --pattern 'sop:*' DEL …`              | `db.drop()`                    |

---

## 7. End-to-end example — "Follow the 0015 / DUP-POSSIBLE DUPLICATE CLAIMS …"

| Where it lives                                                | Row / node                                                                          |
|---------------------------------------------------------------|-------------------------------------------------------------------------------------|
| `auditprecondition` (pk 811)                                  | The raw paragraph text under "Duplicate Exceptions".                                |
| `auditdecision` (pk 593, 594, 595)                            | LLM-extracted Step-0 decision rows promoting the rule into the decision matrix.     |
| `auditstep` (pk 217, step #0)                                 | Synthetic "Pre-Step Exceptions & Override Rules" parent step for the above.         |
| `auditgraphnode` (pk 1490, `pre_7_r1`, type `PRE_RULE`)       | Graph node — `details.action` carries the full sentence.                            |
| `auditgraphedge`                                              | `doc -HAS_PRE_SECTION-> pre_7 -HAS_RULE-> pre_7_r1 -OVERRIDES-> step_7`.            |
| Neo4j (`:PRE_RULE:GraphNode {node_key:'pre_7_r1'}`)           | Mirror of the same node, same parent chain.                                         |
| Redis `sop:graph:{job_id}:pre_sections`                       | The exact LLM output that produced this node (debug artefact, 24 h TTL).            |
| Mongo `sop_parsed_documents`                                  | The full parsed state of the source HTML, including the original pre-section block. |
