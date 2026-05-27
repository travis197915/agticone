# Ingestion pipeline

The `uhc-sop-ingestion` package, end to end.

This is the LangGraph state machine that turns an SOP URL into structured Postgres rows, a Neo4j graph, Mongo snapshots, and Redis cache entries. 17 stages, 122 agent functions, 1 BFS loop.

For per-agent reference see [AGENTS.md](AGENTS.md).

---

## 1. Entry point

```python
from uhc_sop_ingestion import SopIngestionPipeline

pipeline = SopIngestionPipeline()           # auto-loads .env
result = pipeline.run(
    seed_url="https://example.com/sop.html",
    job_id="...",                            # optional, generated if omitted
    max_depth=4,
    max_docs=200,
)
print(result["final_summary"])
```

`pipeline.stream(...)` is also available — yields `(node_name, delta)` tuples for live progress.

Source: [uhc-sop-ingestion/src/uhc_sop_ingestion/pipeline.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/pipeline.py).

When called from Django, the `IngestionJob` row is created first and `job_id` is passed in so the pipeline writes against the existing row. See [sop_ingestion/tasks.py](../sop_ingestion/tasks.py).

---

## 2. State

The LangGraph state is a `TypedDict` (`PipelineState`) declared in [state.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/state.py). Three notable accumulator fields use `Annotated[list, operator.add]` so partial updates append rather than overwrite:

* `url_queue` — BFS queue.
* `visited_hashes`, `visited_urls` — dedup.
* `all_documents` — one summary dict per processed document.
* `errors` — `{"agent": <name>, "msg": <str>}` entries.

Everything else uses the standard "last writer wins" semantics. Critically, **agents return only the keys they change**, and the framework merges the deltas.

---

## 3. Stage map

The graph compiles into 17 nodes wired as follows (see [graph.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/graph.py)):

```
START
  │
intake_stage  ── 5 agents
  │
pick_next_url ── 1 agent  ── (queue empty?) ──► final_stage ► END
  │
fetch_stage   ── 9 agents ── (duplicate? format?)
  │                    │
  │                    └─► link_stage (skip parsing)
  │
  ├── HTML → html_parse  ── 12 agents
  ├── DOCX → docx_parse  ── 6
  ├── XLSX → xlsx_parse  ── 6
  └── PDF  → pdf_parse   ── 3
  │
enrich_stage           ── 10 LLM agents
  │
context_stage          ── 12 LLM code extractors
  │
validate_stage         ── 6 sanity checks
  │
narrative_stage        ── 2 LLM narrators
  │
graph_synthesis_stage  ── 8 LLM agents (build unified graph)
  │
write_neo4j            ── 14 writers
  │
write_postgres         ── 10 writers
  │
write_mongo            ── 3 writers
  │
write_redis            ── 3 writers
  │
link_stage             ── 8 link routers (enqueue new URLs)
  │
completion_check       ── 3 control agents
  │   (more work?)
  └───► pick_next_url   (loop back)
  │
  └───► final_stage     ── 2 agents
            │
           END
```

**Total: 122 agent functions across 17 modules / 16 LangGraph stages.**

---

## 4. Stage breakdown

### 4.1 `intake_stage` (5 agents)

Validates and normalises the seed URL, generates `job_id`, seeds the BFS queue, registers the job in Redis and MongoDB.

Module: [a01_intake.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a01_intake.py).

### 4.2 `pick_next_url` (1 agent)

Pops the first unvisited URL from `url_queue` into `current_url`. Sets `processing_complete=True` when the queue is empty (which routes to `final_stage`).

Module: [a02_fetch.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a02_fetch.py) → `next_url_picker`.

### 4.3 `fetch_stage` (8 remaining fetch agents)

Depth-checks the current URL, fetches via `requests` (or local file), detects format three ways (Content-Type header → file extension → magic bytes), hashes the content, and skips duplicates.

After this stage, conditional routing fires:

* `is_duplicate=True` → `link_stage` (no parse, but still discover outbound links from prior runs).
* Otherwise → `html_parse | docx_parse | xlsx_parse | pdf_parse` based on `doc_format`.

### 4.4 Parse stages

Format-specific extraction. Each one writes into the same set of state keys: `metadata`, `pre_sections`, `steps`, `sub_procedures`, `reference_tables`, `group_rules`, `annotations`, `links`, `raw_text`.

* **`html_parse`** (12 agents) — `a03_parse_html.py`. The heaviest parser. Detects step tables by consecutive-integer column pattern, 2-col or 3-col If/Then tables by content keywords, group-rule tables, sub-procedures, document-level annotations, references, and outbound links. **No CSS-class assumptions** — purely visual / content heuristics, which makes it robust across different SOP authoring tools.

* **`docx_parse`** (6 agents) — `a04_parse_docx.py`. Headings → sections, paragraphs, tables (classified by header content), code tables (with `_infer_code_system`), hyperlinks.

* **`xlsx_parse`** (6 agents) — `a05_parse_xlsx.py`. Classifies the workbook (`CODE_TABLE` / `CALCULATOR_TOOL` / `GENERIC`), parses every sheet, validates header detection, extracts codes, captures calculator metadata.

* **`pdf_parse`** (3 agents) — `a06_parse_pdf.py`. `pypdf` text extraction + XMP metadata + whitespace normalisation. Flags pages with no extractable text (probable scanned image).

### 4.5 `enrich_stage` (10 LLM agents)

LLM polish over the parsed structure.

Module: [a07_enrich.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a07_enrich.py).

Key helper: `_llm_call()` provides retry × 2 → cross-provider fallback → schema validation → `LLMCallLog` row. Every other LLM agent in the pipeline uses it.

Agents:

1. `step_question_refiner` — fills in missing or vague step questions from the surrounding context.
2. `decision_row_classifier` — classifies each If/Then row as DENY / ALLOW / PEND / BYPASS / etc.
3. `rule_semantic_enricher` — adds `action_summary`, `action_line`, `action_claim` (line vs claim-level overrides).
4. `cross_reference_resolver` — disambiguates "see Step 5" / "see appendix B" references.
5. `ambiguous_term_resolver` — interpolates terms like "the system" / "the unit" against the document's context.
6. `potf_validator` — flags "Proof Of Timely Filing" rows missing required fields.
7. `pre_section_rule_extractor` — turns pre-section text into structured `{condition, action, decision_type}` rules.
8. `group_rule_extractor` — normalises group-rule tables into `AuditGroupLimit` rows.
9. `date_condition_extractor` — pulls "for DOS on or after …" rules out of free text.
10. `summary_generator` — writes `purpose` + `llm_summary`.

### 4.6 `context_stage` (12 LLM code extractors)

Find every claims code mentioned anywhere in the document.

Module: [a08_context.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a08_context.py).

**Note on architecture.** Originally this stage had one regex agent per code system (EOB, EX, denial, POS, revenue, bill type, modifier, frequency, system action, CPT). The current implementation makes a **single** OpenAI `json_object` call that returns all code types together, classified by the LLM. `_llm_extract_codes()` does the work; the 10 per-system agents are kept as no-ops in the wiring so the stage's identity is preserved and the call-site list in `graph.py` reads the same as before.

The two non-no-op agents are:

* `eob_code_detector` — actually invokes `_llm_extract_codes` (which returns *all* code types).
* `entity_list_ref_detector` — separate OpenAI call to detect entity list references (e.g. "see attached spreadsheet").
* `code_deduplicator` — merges duplicates by `(code_value, code_type)`, keeping the longest description.

### 4.7 `validate_stage` (6 sanity checks)

Pure-Python sanity checks. Each appends warnings to `validation_warnings` (no failures abort the pipeline).

Module: [a09_validate.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a09_validate.py).

1. `document_completeness` — confirms title, raw_text, ≥ 1 step.
2. `step_sequence` — checks step numbers are contiguous starting at 1.
3. `decision_row_check` — every step has ≥ 1 decision row.
4. `code_system_check` — every detected code has a `code_system`.
5. `link_validator` — discovered links resolve to absolute URLs.
6. `metadata_validator` — `effective_date` / `revision_date` parse as dates if present.

### 4.8 `narrative_stage` (2 LLM narrators)

Story-style summaries the SPA shows in the rule picker.

Module: [a17_narrative.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a17_narrative.py).

* `sop_overview_narrator` — 5–8 sentence executive narrative. Persisted to `AuditSop.narrative_context`.
* `step_narrative_writer` — 2–3 sentence paragraph per step (batched in one LLM call). Persisted to `AuditStep.narrative_context`.

### 4.9 `graph_synthesis_stage` (8 LLM agents)

Builds the unified knowledge graph that lands in `AuditGraphNode` + `AuditGraphEdge` (and is mirrored to Neo4j).

Module: [a16_graph_synthesis.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a16_graph_synthesis.py).

The 8 agents share state through Redis keys (`sop:graph:<job>:<section>`), letting each one work off the previous agents' contributions without depending on LangGraph state surface area. The final `agent_graph_assembler` merges them, runs `sanitize_graph` (drops malformed LLM output) and `_validate_graph` (canonical type / rel names), and either:

* attaches the LLM graph to state (`audit_graph_source = "agentic_llm"`), or
* falls back to a deterministic builder (`audit_graph_source = "deterministic_fallback"`) so downstream writers always have a graph.

Agents:

1. `agent_document_profiler` — `DOCUMENT` (god) node + `META` node.
2. `agent_pre_section_synthesizer` — `PRE_SECTION` + `PRE_RULE` nodes.
3. `agent_step_decomposer` — `STEP` nodes + step-level annotations.
4. `agent_decision_classifier` — `DECISION` nodes + `[:HAS_DECISION]` + `[:GOTO]` edges.
5. `agent_code_grounder` — `CODE` nodes + `[:USES_CODE]` edges.
6. `agent_reference_resolver` — `REFERENCE` / `GROUP_LIMIT` / `DATE_COND` / `ANNOTATION` nodes.
7. `agent_semantic_edge_reasoner` — Claude infers cross-cutting edges (e.g. `OVERRIDES`, `IMPLIES`).
8. `agent_graph_assembler` — merge + validate + state attach.

### 4.10 `write_neo4j` (14 writers)

Hydrates Neo4j from the parsed/enriched state. Each writer is idempotent (`MERGE` on stable IDs).

Module: [a10_write_neo4j.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a10_write_neo4j.py).

* `god_node_writer` — `(:Document)`.
* `pre_section_node_writer` — `(:PreSection)`.
* `step_node_writer` — `(:Step)`.
* `rule_node_writer` — `(:Decision)`.
* `annotation_node_writer` — `(:Annotation)`.
* `code_node_writer` — `(:Code)`.
* `group_rule_node_writer` — `(:GroupRule)`.
* `sequential_edge_writer` — `(:Step)-[:NEXT]->(:Step)`.
* `branch_edge_writer` — `(:Step)-[:IF_YES|IF_NO|GOTO]->(:Step)`.
* `child_doc_edge_writer` — `(:Document)-[:LINKS_TO]->(:Document)` for cross-SOP refs.
* `reference_table_node_writer` — `(:ReferenceTable)` for POTF / code tables.
* `group_rule_step_edge_writer` — `(:Step)-[:HAS_GROUP_RULE]->(:GroupRule)`.
* `sub_procedure_node_writer` — `(:SubProcedure)` (ERB-style sub-flows).
* `neo4j_graph_writer` — final pass that materialises the **canonical** `AuditGraphNode` / `AuditGraphEdge` set into Neo4j (the single source of truth from `graph_synthesis_stage`).

### 4.11 `write_postgres` (10 writers)

Writes the canonical relational record.

Module: [a11_write_postgres.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a11_write_postgres.py).

Uses raw psycopg2 (not the Django ORM) for two reasons: (a) the pipeline runs in a Celery worker that may not have Django bootstrapped, (b) bulk inserts are dramatically faster as `executemany`. Every write strips NUL bytes from JSONB values (Postgres rejects them) and clamps text length where the column has a `max_length`.

* `pg_sop_writer` — upserts `AuditSop`, captures `sop_db_id` into state.
* `pg_precondition_writer` — `AuditPrecondition` rows.
* `pg_step_writer` — `AuditStep` + `AuditDecision` rows.
* `pg_group_limit_writer` — `AuditGroupLimit`.
* `pg_code_writer` — `AuditCode`.
* `pg_date_condition_writer` — `AuditDateCondition`.
* `pg_annotation_writer` — `AuditAnnotation`.
* `pg_reference_writer` — `AuditReference`.
* `pg_graph_writer` — `AuditGraphNode` + `AuditGraphEdge` (the canonical graph).
* `pg_job_updater` — increments `IngestionJob.docs_processed`.

### 4.12 `write_mongo` (3 writers)

Append-only audit log per document.

Module: [a12_write_mongo.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a12_write_mongo.py).

* `mongo_raw_writer` — `raw_documents` collection: raw bytes + URL + content hash + fetch metadata.
* `mongo_parsed_writer` — `parsed_documents` collection: full schema-free parse JSON.
* `mongo_job_progress` — increments counters on the `ingestion_jobs` document.

### 4.13 `write_redis` (3 writers)

Hot cache for live UIs.

Module: [a13_write_redis.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a13_write_redis.py).

* `redis_cache_writer` — parsed doc summary, 1-hour TTL.
* `redis_queue_manager` — persists current BFS queue (crash recovery).
* `redis_progress_tracker` — updates the live job-progress hash.

### 4.14 `link_stage` (8 link routers)

Classify, dedupe, and enqueue outbound links from the just-processed document.

Module: [a14_links.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a14_links.py).

* `link_classifier` — assigns `link_type` to any link with blank / `UNKNOWN`.
* `html_link_queue`, `docx_link_queue`, `xlsx_link_queue`, `pdf_link_queue` — per-format BFS append.
* `unresolved_link_logger` — `href="#"` and similar dead links logged to Redis.
* `internal_anchor_mapper` — `#step-5` → step number mapping.
* `accumulated_doc_appender` — pushes a summary dict to `all_documents`.

### 4.15 `completion_check` (3 control agents)

Module: [a15_control.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a15_control.py).

* `error_handler` — flushes accumulated errors to Redis; marks job `PARTIAL` if any.
* `completion_checker` — sets `processing_complete=True` when the BFS queue is empty.
* `state_clearer` — wipes per-document state keys so the next iteration starts clean.

### 4.16 `final_stage` (2 agents)

* `final_summary` — totals across `all_documents`: `total_docs_processed`, `total_rules`, `total_codes`, `total_links`.
* `job_closer` — marks job `COMPLETED` in Redis, MongoDB, and (via the surrounding Django Celery task) Postgres.

---

## 5. Real-time logging

Every stage is wrapped by `_stage()` in [graph.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/graph.py).

```python
def node(state):
    pg_logger = getattr(cfg, "_pg_logger", None)
    row_id = pg_logger.log_stage_start(stage_name, doc_url, doc_format, doc_depth) if pg_logger else None
    try:
        ...                                       # run each agent
        pg_logger.log_stage_end(row_id, status="OK", started_ts=t0)
    except Exception as exc:
        pg_logger.log_stage_end(row_id, status="ERROR", error_detail=str(exc), started_ts=t0)
        raise
```

`PipelineLogger` ([pg_logger.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/pg_logger.py)) opens its own psycopg2 connection in autocommit mode so writes are visible from the HTML viewer **while the job is still running**. It writes:

* One `PipelineStageLog` row per LangGraph node.
* One `LLMCallLog` row per `_llm_call()` invocation.

At the end of the run, `refresh_job_totals()` aggregates `LLMCallLog` into `IngestionJob.total_llm_calls` / `total_tokens_in` / `total_tokens_out`.

---

## 6. Configuration

`PipelineConfig.from_env(env_path)` ([config.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/config.py)) reads:

| Service     | Env vars                                                                  |
|-------------|---------------------------------------------------------------------------|
| Postgres    | `PG_HOST`, `PG_PORT`, `PG_USER`, `PG_PASSWORD`, `PG_DATABASE`.            |
| Redis       | `REDIS_HOST`, `REDIS_PORT`, `REDIS_USER`, `REDIS_PASSWORD`.               |
| Neo4j       | `NEO4J_HOST`, `NEO4J_PORT`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`. |
| MongoDB     | `MONGO_HOST`, `MONGO_PORT`, `MONGO_USER`, `MONGO_PASSWORD`, `MONGO_DATABASE`. |
| LLM         | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `LLM_PROVIDER`, `LLM_MODEL`.        |
| Pipeline    | `MAX_DEPTH` (default 4), `MAX_DOCS` (default 200).                        |

`get_redis(cfg)`, `get_mongo(cfg)`, `get_neo4j(cfg)` are module-level singletons that lazy-connect on first use.

---

## 7. Failure modes

The pipeline is designed to soft-fail and continue.

* **HTTP fetch error** — recorded in `errors`, document is skipped, BFS continues.
* **LLM call error** — retried 2× same provider, then once on the other provider. If both fail, the agent returns a sensible default (often the un-enriched input) and writes a `success=false` `LLMCallLog`.
* **Postgres / Neo4j write error** — agent returns `{"errors": [{"agent": "...", "msg": "..."}]}`; the stage logs `ERROR` in `PipelineStageLog`; the pipeline continues. Job is marked `PARTIAL` at the end if any errors accumulated.
* **Graph synthesis failure** — `agent_graph_assembler` falls back to the deterministic graph builder; `audit_graph_source` is set to `"deterministic_fallback"`.
* **Pipeline crash (uncaught exception)** — the Celery task catches it and marks `IngestionJob.status = FAILED`. Stage logs up to that point survive (autocommit).

---

## 8. Configuration knobs worth knowing

* `MAX_DEPTH=4` — BFS link-hop limit. Set to `1` to ingest only the seed URL.
* `MAX_DOCS=200` — hard upper bound on the number of documents per job.
* `LLM_PROVIDER` / `LLM_MODEL` — change defaults per job via the `POST /api/ingest/` body or per-environment via `.env`.
* `Workflow.metadata` JSONB lets the UI store user-set knobs without a migration.

---

## 9. Replaying / debugging

* **Backfill narratives only** — `POST /api/ingest/<job_id>/contextualize/?sync=true` (no re-fetch).
* **Sync run** — `POST /api/ingest/run-sync/` (DEBUG only) blocks until done, perfect for breakpoints.
* **HTML viewer** — `/api/ingest/viewer/<job_id>/` shows live stage logs, LLM logs, and parsed structures.
* **Stage replay** — there is no built-in resume from a specific stage. Workaround: delete the SOP rows and re-run.
* **Re-ingest the same URL** — duplicates by `content_hash` are skipped. Force a re-ingest by changing the source content or deleting the existing `AuditSop` row.
