# Per-agent reference

Every one of the 122 agents in the `uhc-sop-ingestion` pipeline, grouped by stage.

Each entry lists:

* **Purpose** — one sentence.
* **Reads** — `PipelineState` keys consumed.
* **Writes** — `PipelineState` keys produced.
* **Side effects** — external calls (Redis / Mongo / Neo4j / Postgres / HTTP / LLM).

Conventions used in this doc:

* "writes `errors`" means the agent appends a `{"agent": <Name>, "msg": <str>}` entry on failure. Errors do not abort the pipeline.
* Field names match `PipelineState` in [state.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/state.py).
* Source line numbers are stable as of branch `feat/rule-engine`.

---

## Stage `intake_stage` — `a01_intake.py`

5 agents.

### `url_validator`

**Purpose.** Validates the seed URL is non-empty and has a recognised scheme.
**Reads.** `seed_url`.
**Writes.** `seed_url` (normalised), `errors`.
**Side effects.** None.

### `url_normalizer`

**Purpose.** Strips whitespace, removes `#fragment`, trims trailing `/`.
**Reads.** `seed_url`. **Writes.** `seed_url`.
**Side effects.** None.

### `job_initializer`

**Purpose.** Generates `job_id` (or reuses caller-supplied one) and seeds the BFS queue with the root URL.
**Reads.** `seed_url`, `job_id`, `max_depth`, `max_docs`.
**Writes.** `job_id`, `url_queue`, `visited_urls`, `visited_hashes`, `all_documents`, `errors`, `validation_warnings`, `total_processed`, `processing_complete`, `max_depth`, `max_docs`.
**Side effects.** None.

### `redis_job_tracker`

**Purpose.** Registers the job in Redis as a HASH for live progress tracking.
**Reads.** `job_id`, `seed_url`.
**Writes.** `errors` on failure.
**Side effects.** `HSET sop:job:<job_id>` with status/timestamps; 7-day TTL.

### `mongo_job_logger`

**Purpose.** Inserts the job document into MongoDB for durable audit logging.
**Reads.** `job_id`, `seed_url`, `max_depth`, `max_docs`.
**Writes.** `errors` on failure.
**Side effects.** `ingestion_jobs.insert_one({_id: job_id, ...})`.

---

## Stage `pick_next_url` / `fetch_stage` — `a02_fetch.py`

9 agents (1 for `pick_next_url`, 8 for `fetch_stage`).

### `next_url_picker`

**Purpose.** Pops the first unvisited URL from `url_queue` into `current_url`. Sets `processing_complete` when empty.
**Reads.** `url_queue`, `visited_urls`. **Writes.** `url_queue` (cleared accumulator), `current_url`, `current_depth`, `current_parent_url`, `processing_complete`.

### `depth_limit_checker`

**Purpose.** Skips processing if depth > `max_depth` or total ≥ `max_docs`.
**Reads.** `current_depth`, `max_depth`, `total_processed`, `max_docs`. **Writes.** `is_duplicate` (reused as a skip flag).

### `http_fetcher`

**Purpose.** Fetches a remote URL via `requests`.
**Reads.** `current_url`. **Writes.** `raw_bytes_b64`, `content_type`, `encoding`, `is_local`, `errors`.
**Side effects.** HTTP GET with `User-Agent: UHC-SOP-Ingestion/1.0`, 30 s timeout, 100 MiB body cap, streams chunks.

### `local_file_fetcher`

**Purpose.** Reads a local file path into `raw_bytes_b64`.
**Reads.** `current_url`. **Writes.** `raw_bytes_b64`, `content_type`, `encoding`, `is_local`, `errors`.

### `content_type_detector`

**Purpose.** Maps the HTTP `Content-Type` header to `doc_format` (HTML/DOCX/XLSX/PDF).
**Reads.** `content_type`. **Writes.** `doc_format` (only if matched).

### `extension_detector`

**Purpose.** Detects `doc_format` from the URL file extension (most reliable signal).
**Reads.** `current_url`, `doc_format`. **Writes.** `doc_format` (only if still unset).

### `magic_bytes_detector`

**Purpose.** Sniffs the first 16 bytes to detect PDF (`%PDF`), ZIP-OOXML (DOCX/XLSX) and HTML; sets `UNKNOWN` otherwise.
**Reads.** `raw_bytes_b64`, `doc_format`. **Writes.** `doc_format`.

### `content_hasher`

**Purpose.** Computes SHA-256[:16] of the raw content for dedup.
**Reads.** `raw_bytes_b64`. **Writes.** `content_hash`.

### `duplicate_checker`

**Purpose.** Marks `is_duplicate=True` if the hash or URL was already processed; always records hash + URL as visited.
**Reads.** `content_hash`, `current_url`, `visited_hashes`, `visited_urls`. **Writes.** `is_duplicate`, `visited_hashes` (append), `visited_urls` (append).

---

## Stage `html_parse` — `a03_parse_html.py`

12 agents. The heaviest parser. All work via BeautifulSoup. No CSS-class assumptions — purely visual / content heuristics.

### `html_decode`

**Purpose.** Base64-decodes raw bytes, lets BS4 auto-detect encoding from `<meta charset>`.
**Reads.** `raw_bytes_b64`. **Writes.** `raw_text` (placeholder for downstream).

### `html_metadata`

**Purpose.** Extracts title, effective/revision dates, platform, LOB, audience, etc. from header tables.
**Reads.** soup of `raw_bytes_b64`. **Writes.** `metadata`.

### `html_biz_table`

**Purpose.** Detects and parses the "business" header table (platform / audience / LOB / state).
**Reads.** soup. **Writes.** `metadata` (merged).

### `html_pre_sections`

**Purpose.** Detects every label→content row pattern in non-step / non-business tables → `pre_sections[]`.
**Reads.** soup. **Writes.** `pre_sections`.

### `html_steps`

**Purpose.** Detects step tables purely by consecutive-integer column pattern (e.g. a column whose cells are `1`, `2`, `3`).
**Reads.** soup. **Writes.** `steps` with stub `decision_rows`.

### `html_decision_tables`

**Purpose.** Attaches 2-col `IF → THEN` inner tables to their parent step. Detection is content-based (header keywords).
**Reads.** `steps`, soup. **Writes.** `steps[*].decision_rows`.

### `html_compound_tables`

**Purpose.** Attaches 3-col `IF / AND / THEN` tables (compound conditions).
**Reads.** `steps`, soup. **Writes.** `steps[*].decision_rows`.

### `html_group_tables`

**Purpose.** Detects group-rule tables anywhere in the document (outside step cells).
**Reads.** soup. **Writes.** `group_rules`.

### `html_annotations`

**Purpose.** Collects all document-level annotations (NOTE / ALERT / EXCEPTION / TIP / WARNING / HIGHLIGHT) via inline background-colour heuristic.
**Reads.** soup. **Writes.** `annotations`.

### `html_reference_tables`

**Purpose.** Detects reference tables (POTF code lookups, valid/invalid lists) by content keywords.
**Reads.** soup. **Writes.** `reference_tables`.

### `html_sub_procedures`

**Purpose.** Detects secondary numbered step sequences (sub-procedures like ERB).
**Reads.** soup. **Writes.** `sub_procedures`.

### `html_links`

**Purpose.** Extracts all `<a href>` links with link text and classifies them (HTML_SOP / DOCX / XLSX / PDF / UNRESOLVED).
**Reads.** soup. **Writes.** `links`.

---

## Stage `docx_parse` — `a04_parse_docx.py`

6 agents. All work via `python-docx`.

### `docx_metadata`
**Purpose.** Core properties (title, author, modified date) → `metadata`.
**Reads.** `raw_bytes_b64`. **Writes.** `metadata`.

### `docx_headings`
**Purpose.** Parses `Heading 1..6` styles into a section tree.
**Reads.** docx. **Writes.** `pre_sections` (each heading becomes a section).

### `docx_paragraphs`
**Purpose.** Flat paragraph text into `raw_text` and per-section items.
**Reads.** docx. **Writes.** `raw_text`, `pre_sections[*].items`.

### `docx_tables`
**Purpose.** Classifies each table by header content → step / decision / group / annotation / code / generic.
**Reads.** docx. **Writes.** `steps`, `group_rules`, `annotations` as applicable.

### `docx_code_tables`
**Purpose.** Specifically extracts code-system tables (EOB / EX / denial etc.) using `_infer_code_system` heuristics.
**Reads.** docx tables. **Writes.** `code_table_entries`.

### `docx_hyperlinks`
**Purpose.** Pulls every hyperlink (`<w:hyperlink>`) into `links`.
**Reads.** docx. **Writes.** `links`.

---

## Stage `xlsx_parse` — `a05_parse_xlsx.py`

6 agents. All work via `openpyxl`.

### `xlsx_workbook_type`
**Purpose.** Classifies the workbook as `CODE_TABLE` / `CALCULATOR_TOOL` / `GENERIC` based on sheet names + first-row content.
**Reads.** `raw_bytes_b64`. **Writes.** `xlsx_workbook_type`.

### `xlsx_sheet_parser`
**Purpose.** Parses every sheet into `reference_tables[]`.
**Reads.** workbook. **Writes.** `reference_tables`.

### `xlsx_header_detector`
**Purpose.** Validates that the first sheet has a clear header row; logs warning otherwise.
**Reads.** workbook. **Writes.** `parse_warnings`.

### `xlsx_code_extractor`
**Purpose.** Extracts claims-code rows from `CODE_TABLE` / `GENERIC` workbooks.
**Reads.** workbook, `xlsx_workbook_type`. **Writes.** `code_table_entries`.

### `xlsx_calculator`
**Purpose.** Captures formulas / calc-tool metadata for `CALCULATOR_TOOL` workbooks.
**Reads.** workbook, `xlsx_workbook_type`. **Writes.** `metadata`.

### `xlsx_metadata`
**Purpose.** Sheet count, workbook properties → `metadata`.
**Reads.** workbook. **Writes.** `metadata`.

---

## Stage `pdf_parse` — `a06_parse_pdf.py`

3 agents. All work via `pypdf`.

### `pdf_text_extractor`
**Purpose.** Page-by-page text. Flags pages with no extractable text (probable scanned image).
**Reads.** `raw_bytes_b64`. **Writes.** `pre_sections`, `raw_text`, `parse_warnings`.

### `pdf_metadata`
**Purpose.** XMP / Info dictionary → `metadata` (`title`, `effective_date`, `revision_date`).
**Reads.** `raw_bytes_b64`, `current_url`, `metadata`. **Writes.** `metadata`.

### `pdf_raw_text_normalizer`
**Purpose.** Collapses repeated blank lines and runs of whitespace; warns if extracted text < 500 chars.
**Reads.** `raw_text`. **Writes.** `raw_text`, `parse_warnings`.

---

## Stage `enrich_stage` — `a07_enrich.py`

10 LLM agents. All route through `_llm_call()` which provides retry × 2 → cross-provider fallback → schema validation → `LLMCallLog` row.

### `step_question_refiner`
**Purpose.** Fills in missing or vague step questions using surrounding text.
**Reads.** `steps`, `metadata`. **Writes.** `enriched_steps`.
**LLM.** Claude — free-text refinement.

### `decision_row_classifier`
**Purpose.** Classifies each If/Then row's `decision_type` (DENY / ALLOW / PEND / BYPASS / REFER / SYSTEM / STOP / WAIVE / CONDITIONAL).
**Reads.** `enriched_steps` or `steps`. **Writes.** `enriched_steps`.
**LLM.** Claude — JSON output schema-validated.

### `rule_semantic_enricher`
**Purpose.** Adds `action_summary`, `action_line`, `action_claim` (one-line + line-level + claim-level summaries).
**Reads.** `enriched_steps`. **Writes.** `enriched_steps`.
**LLM.** Claude.

### `cross_reference_resolver`
**Purpose.** Disambiguates "see Step 5" / "see appendix B" references.
**Reads.** `enriched_steps`. **Writes.** `enriched_steps`.
**LLM.** Claude.

### `ambiguous_term_resolver`
**Purpose.** Interpolates ambiguous terms ("the system", "the unit") against the document's metadata.
**Reads.** `enriched_steps`, `metadata`. **Writes.** `enriched_steps`.
**LLM.** Claude.

### `potf_validator`
**Purpose.** Flags POTF (Proof Of Timely Filing) rows missing required fields.
**Reads.** `enriched_steps`. **Writes.** `validation_warnings`.
**LLM.** Claude.

### `pre_section_rule_extractor`
**Purpose.** Turns pre-section text into structured `{condition, action, decision_type, is_exception}` rules stored on `pre_sections[*].llm_rules`.
**Reads.** `pre_sections`. **Writes.** `pre_sections`.
**LLM.** OpenAI GPT-4o `json_object`.

### `group_rule_extractor`
**Purpose.** Normalises group-rule tables into structured group-limit objects (`inn_days`, `oon_days`, basis, exceptions).
**Reads.** `group_rules`. **Writes.** `group_rules` (enriched).
**LLM.** OpenAI GPT-4o `json_object`.

### `date_condition_extractor`
**Purpose.** Pulls "for DOS on or after …" style date conditions out of free text.
**Reads.** `raw_text` (first 5000 chars). **Writes.** `detected_date_conditions`.
**LLM.** OpenAI GPT-4o `json_object`.

### `summary_generator`
**Purpose.** Writes `purpose` (one-liner) and `llm_summary` (3–4 sentence executive summary).
**Reads.** `metadata`, `enriched_steps`, `pre_sections`. **Writes.** `metadata.purpose`, `llm_summary`.
**LLM.** Claude.

---

## Stage `context_stage` — `a08_context.py`

12 agents. All but two are no-op shims (architecture note in [PIPELINE.md §4.6](PIPELINE.md#46-context_stage-12-llm-code-extractors)).

### `eob_code_detector`
**Purpose.** **The** code detector — single OpenAI `json_object` call that returns *every* code type for the whole document.
**Reads.** all parsed text. **Writes.** `detected_codes`.
**LLM.** OpenAI GPT-4o.

### `ex_code_detector` / `denial_code_detector` / `pos_code_detector` / `revenue_code_detector` / `bill_type_detector` / `modifier_code_detector` / `frequency_code_detector` / `system_action_detector` / `cpt_code_detector`
**Purpose.** No-ops kept to preserve the stage's identity; their work was consolidated into `eob_code_detector`.
**Reads.** none. **Writes.** `{}`.

### `entity_list_ref_detector`
**Purpose.** Separate OpenAI call to detect entity list references ("see attached spreadsheet of valid codes").
**Reads.** all parsed text. **Writes.** `detected_list_refs`.
**LLM.** OpenAI GPT-4o.

### `code_deduplicator`
**Purpose.** Merges duplicates by `(code_value, code_type)`, keeping the longest description.
**Reads.** `detected_codes`. **Writes.** `detected_codes`.

---

## Stage `validate_stage` — `a09_validate.py`

6 agents. Pure-Python sanity checks; all append to `validation_warnings`, none abort.

### `document_completeness`
**Purpose.** Title present, raw_text non-empty, ≥ 1 step.
**Reads.** `metadata`, `raw_text`, `steps`. **Writes.** `validation_warnings`, `validation_passed`.

### `step_sequence`
**Purpose.** Step numbers are contiguous starting at 1.
**Reads.** `enriched_steps` or `steps`. **Writes.** `validation_warnings`.

### `decision_row_check`
**Purpose.** Every step has ≥ 1 decision row.
**Reads.** `enriched_steps`. **Writes.** `validation_warnings`.

### `code_system_check`
**Purpose.** Every detected code has a `code_system` set.
**Reads.** `detected_codes`. **Writes.** `validation_warnings`.

### `link_validator`
**Purpose.** Discovered links resolve to absolute URLs.
**Reads.** `links`, `current_url`. **Writes.** `validation_warnings`.

### `metadata_validator`
**Purpose.** `effective_date` / `revision_date` parse if present.
**Reads.** `metadata`. **Writes.** `validation_warnings`.

---

## Stage `narrative_stage` — `a17_narrative.py`

2 LLM agents.

### `sop_overview_narrator`
**Purpose.** Writes a 5–8 sentence executive narrative for the whole SOP (audience, walkthrough, notable codes).
**Reads.** `metadata`, `pre_sections`, `enriched_steps`, `llm_summary`. **Writes.** `metadata.narrative` (persisted to `AuditSop.narrative_context`).
**LLM.** Claude.

### `step_narrative_writer`
**Purpose.** Generates a 2–3 sentence paragraph **per step**, batched into one LLM call.
**Reads.** `enriched_steps`. **Writes.** `enriched_steps[*].narrative_context`.
**LLM.** Claude.

---

## Stage `graph_synthesis_stage` — `a16_graph_synthesis.py`

8 LLM agents. Share state via Redis keys `sop:graph:<job>:<section>`.

### `agent_document_profiler`
**Purpose.** Emits the DOCUMENT (god) node + a METADATA node.
**Reads.** `metadata`. **Writes.** Redis `sop:graph:<job>:document`.
**LLM.** Claude.

### `agent_pre_section_synthesizer`
**Purpose.** Emits PRE_SECTION nodes + PRE_RULE child nodes for every distinct rule.
**Reads.** `pre_sections`. **Writes.** Redis `sop:graph:<job>:pre_sections`.
**LLM.** Claude.

### `agent_step_decomposer`
**Purpose.** Emits STEP nodes + step-level ANNOTATION children + branch annotations.
**Reads.** `enriched_steps`. **Writes.** Redis `sop:graph:<job>:steps`.
**LLM.** Claude.

### `agent_decision_classifier`
**Purpose.** Emits DECISION nodes per step + `[:HAS_DECISION]` + `[:GOTO]` edges.
**Reads.** `enriched_steps`. **Writes.** Redis `sop:graph:<job>:decisions`.
**LLM.** Claude.

### `agent_code_grounder`
**Purpose.** Emits CODE nodes + `[:USES_CODE]` edges from decisions to codes.
**Reads.** `detected_codes`, decisions context. **Writes.** Redis `sop:graph:<job>:codes`.
**LLM.** Claude.

### `agent_reference_resolver`
**Purpose.** Emits REFERENCE / GROUP_LIMIT / DATE_COND / ANNOTATION nodes.
**Reads.** `links`, `group_rules`, `detected_date_conditions`, `annotations`. **Writes.** Redis `sop:graph:<job>:refs`.
**LLM.** Claude.

### `agent_semantic_edge_reasoner`
**Purpose.** Infers cross-cutting semantic edges (OVERRIDES, IMPLIES, etc.) between nodes from earlier agents.
**Reads.** Redis context from all earlier agents. **Writes.** Redis `sop:graph:<job>:semantic_edges`.
**LLM.** Claude.

### `agent_graph_assembler`
**Purpose.** Merges every agent's contribution, runs `sanitize_graph` + `_validate_graph`, attaches to state. Falls back to deterministic graph builder if validation fails.
**Reads.** All `sop:graph:<job>:*` Redis keys. **Writes.** `audit_graph_nodes`, `audit_graph_edges`, `audit_graph_source` (`"agentic_llm"` or `"deterministic_fallback"`).

---

## Stage `write_neo4j` — `a10_write_neo4j.py`

14 writers. Each is idempotent via Cypher `MERGE` on stable IDs (e.g. `sop_id + node_key`).

### `god_node_writer`
**Purpose.** `(:Document)` root node.
**Reads.** `current_url`, `metadata`, `content_hash`. **Writes.** `neo4j_sop_id`.
**Side effects.** Neo4j MERGE.

### `pre_section_node_writer`
**Purpose.** `(:PreSection)` nodes + `(:Document)-[:HAS_PRE_SECTION]->(:PreSection)`.
**Reads.** `neo4j_sop_id`, `pre_sections`. **Side effects.** Neo4j MERGE.

### `step_node_writer`
**Purpose.** `(:Step)` nodes.
**Reads.** `neo4j_sop_id`, `enriched_steps`. **Side effects.** Neo4j MERGE.

### `rule_node_writer`
**Purpose.** `(:Decision)` (rule) nodes.
**Reads.** `neo4j_sop_id`, `enriched_steps`. **Side effects.** Neo4j MERGE.

### `annotation_node_writer`
**Purpose.** `(:Annotation)` nodes.
**Reads.** `neo4j_sop_id`, `annotations`. **Side effects.** Neo4j MERGE.

### `code_node_writer`
**Purpose.** `(:Code)` nodes per detected code.
**Reads.** `neo4j_sop_id`, `detected_codes`. **Side effects.** Neo4j MERGE.

### `group_rule_node_writer`
**Purpose.** `(:GroupRule)` nodes for timely-filing groups.
**Reads.** `neo4j_sop_id`, `group_rules`. **Side effects.** Neo4j MERGE.

### `sequential_edge_writer`
**Purpose.** `(:Step)-[:NEXT]->(:Step)` chaining.
**Reads.** `neo4j_sop_id`, `enriched_steps`. **Side effects.** Neo4j MERGE.

### `branch_edge_writer`
**Purpose.** `(:Step)-[:IF_YES|IF_NO|GOTO]->(:Step)` edges from decision rows.
**Reads.** `neo4j_sop_id`, `enriched_steps`. **Side effects.** Neo4j MERGE.

### `child_doc_edge_writer`
**Purpose.** `(:Document)-[:LINKS_TO]->(:Document)` for cross-SOP references.
**Reads.** `neo4j_sop_id`, `links`. **Side effects.** Neo4j MERGE.

### `reference_table_node_writer`
**Purpose.** `(:ReferenceTable)` nodes (Valid POTF / Invalid POTF / code tables).
**Reads.** `neo4j_sop_id`, `reference_tables`. **Side effects.** Neo4j MERGE.

### `group_rule_step_edge_writer`
**Purpose.** `(:Step)-[:HAS_GROUP_RULE]->(:GroupRule)` so group rules can be traced back to the step that applies them.
**Reads.** `neo4j_sop_id`, `group_rules`, `enriched_steps`. **Side effects.** Neo4j MERGE.

### `sub_procedure_node_writer`
**Purpose.** `(:SubProcedure)` nodes (ERB-style sub-flows) with their steps as children.
**Reads.** `neo4j_sop_id`, `sub_procedures`. **Side effects.** Neo4j MERGE.

### `neo4j_graph_writer`
**Purpose.** Final pass — materialises the **canonical** `AuditGraphNode` / `AuditGraphEdge` set into Neo4j as `(:GraphNode)` + edges with `details` flattened (Neo4j doesn't support nested properties).
**Reads.** `neo4j_sop_id`, `audit_graph_nodes`, `audit_graph_edges`. **Side effects.** Neo4j MERGE.

---

## Stage `write_postgres` — `a11_write_postgres.py`

10 writers. Raw psycopg2 (not Django ORM). Every writer strips NUL bytes from JSONB values and clamps text length.

### `pg_sop_writer`
**Purpose.** Upserts `AuditSop` for the current document.
**Reads.** `job_id`, `current_url`, `content_hash`, `metadata`, `llm_summary`, `raw_text`, etc.
**Writes.** `sop_db_id` (so child writers can FK it).
**Side effects.** Postgres `INSERT ... ON CONFLICT (job_id, content_hash) DO UPDATE`.

### `pg_precondition_writer`
**Purpose.** Bulk-writes `AuditPrecondition` rows.
**Reads.** `sop_db_id`, `pre_sections`. **Side effects.** Postgres `executemany`.

### `pg_step_writer`
**Purpose.** Bulk-writes `AuditStep` + `AuditDecision` rows. Classifies any unclassified codes via `_classify_code` fallback.
**Reads.** `sop_db_id`, `enriched_steps`. **Side effects.** Postgres `executemany`.

### `pg_group_limit_writer`
**Purpose.** Bulk-writes `AuditGroupLimit`.
**Reads.** `sop_db_id`, `group_rules`. **Side effects.** Postgres `executemany`.

### `pg_code_writer`
**Purpose.** Bulk-writes `AuditCode` with `ON CONFLICT DO NOTHING` on `(sop, code_value, code_type)`.
**Reads.** `sop_db_id`, `detected_codes`. **Side effects.** Postgres `executemany`.

### `pg_date_condition_writer`
**Purpose.** Bulk-writes `AuditDateCondition`.
**Reads.** `sop_db_id`, `detected_date_conditions`. **Side effects.** Postgres `executemany`.

### `pg_annotation_writer`
**Purpose.** Bulk-writes `AuditAnnotation` (linked to step if step-scoped).
**Reads.** `sop_db_id`, `annotations`, step IDs. **Side effects.** Postgres `executemany`.

### `pg_reference_writer`
**Purpose.** Bulk-writes `AuditReference`.
**Reads.** `sop_db_id`, `links`. **Side effects.** Postgres `executemany`.

### `pg_graph_writer`
**Purpose.** Persists the canonical SOP knowledge graph into `AuditGraphNode` + `AuditGraphEdge`. Idempotent — deletes existing rows for the SOP first, then bulk-inserts.
**Reads.** `sop_db_id`, `audit_graph_nodes`, `audit_graph_edges`. **Writes.** `audit_graph_persisted_nodes`, `audit_graph_persisted_edges`.
**Side effects.** Postgres `DELETE` + `executemany INSERT`.

### `pg_job_updater`
**Purpose.** Increments `IngestionJob.docs_processed` after a document is written.
**Reads.** `job_id`. **Side effects.** Postgres `UPDATE`.

---

## Stage `write_mongo` — `a12_write_mongo.py`

3 writers. All target the `MONGO_DATABASE` from `.env`.

### `mongo_raw_writer`
**Purpose.** Stores raw bytes, content hash, URL, fetch metadata for replay.
**Reads.** `raw_bytes_b64`, `current_url`, `content_hash`, `doc_format`, `content_type`. **Writes.** `mongo_raw_id`.
**Side effects.** `raw_documents.insert_one`.

### `mongo_parsed_writer`
**Purpose.** Stores the full structured parse output as schema-free JSON.
**Reads.** All parse state. **Writes.** `mongo_parsed_id`.
**Side effects.** `parsed_documents.insert_one`.

### `mongo_job_progress`
**Purpose.** Increments counters on the job document (`docs_processed`, `rules_count`, etc.).
**Reads.** `job_id`. **Side effects.** `ingestion_jobs.update_one({$inc: ...})`.

---

## Stage `write_redis` — `a13_write_redis.py`

3 writers.

### `redis_cache_writer`
**Purpose.** Caches the parsed-document summary (without raw bytes) for 1 hour.
**Reads.** Parse state. **Side effects.** `SET sop:doc:<hash>` with EX=3600.

### `redis_queue_manager`
**Purpose.** Persists the current BFS queue to Redis for crash recovery.
**Reads.** `url_queue`, `job_id`. **Side effects.** `SET sop:queue:<job>`.

### `redis_progress_tracker`
**Purpose.** Updates the live job-progress hash (`docs_processed`, `current_url`, etc.).
**Reads.** `job_id`, `total_processed`, `current_url`. **Side effects.** `HSET sop:job:<job>`.

---

## Stage `link_stage` — `a14_links.py`

8 link routers.

### `link_classifier`
**Purpose.** Re-classifies any link whose `link_type` is blank or `UNKNOWN`.
**Reads.** `links`. **Writes.** `links`.

### `html_link_queue` / `docx_link_queue` / `xlsx_link_queue` / `pdf_link_queue`
**Purpose.** Per-format BFS append. Each enqueues only links of its declared format, dedups against `visited_urls`, and respects `max_depth`.
**Reads.** `links`, `visited_urls`, `current_depth`, `max_depth`. **Writes.** `url_queue` (append).

### `unresolved_link_logger`
**Purpose.** Logs UNRESOLVED links (`href="#"`, dead anchors) to Redis for later resolution.
**Reads.** `links`, `job_id`. **Side effects.** `LPUSH sop:unresolved:<job>`.

### `internal_anchor_mapper`
**Purpose.** Stores internal `#anchor` links → step/section mapping in Redis for cross-ref resolution.
**Reads.** `links`, `enriched_steps`, `job_id`. **Side effects.** `HSET sop:anchors:<job>`.

### `accumulated_doc_appender`
**Purpose.** Appends a summary dict (`url`, `content_hash`, `doc_format`, counts) to `all_documents`.
**Reads.** Parse + write state. **Writes.** `all_documents` (append), `total_processed` (incremented).

---

## Stage `completion_check` — `a15_control.py`

3 control agents (plus 2 in `final_stage`).

### `error_handler`
**Purpose.** Logs accumulated errors to Redis; marks job `PARTIAL` if any.
**Reads.** `errors`, `job_id`. **Side effects.** `LPUSH sop:errors:<job>`; updates job hash.

### `completion_checker`
**Purpose.** Sets `processing_complete=True` when `url_queue` is exhausted.
**Reads.** `url_queue`. **Writes.** `processing_complete`.

### `state_clearer`
**Purpose.** Resets per-document state keys (`raw_bytes_b64`, `doc_format`, `content_hash`, `metadata`, etc.) so the next BFS iteration starts clean.
**Reads.** All current-doc keys. **Writes.** them back as empty/null.

---

## Stage `final_stage` — `a15_control.py`

2 agents.

### `final_summary`
**Purpose.** Builds the run-level summary used by the API response: `total_docs_processed`, `total_rules`, `total_codes`, `total_links`, error counts.
**Reads.** `all_documents`, `errors`. **Writes.** `final_summary`.

### `job_closer`
**Purpose.** Marks the job `COMPLETED` (or `PARTIAL`) in Redis and Mongo. Postgres `IngestionJob.status` is updated by the surrounding Celery task in [sop_ingestion/tasks.py](../sop_ingestion/tasks.py) once the pipeline returns.
**Reads.** `job_id`, `final_summary`, `errors`. **Side effects.** Redis `HSET`, Mongo `update_one`.

---

## Agent count summary

| Stage                    | File                         | Count |
|--------------------------|------------------------------|------:|
| `intake_stage`           | a01_intake.py                | 5     |
| `pick_next_url`          | a02_fetch.py                 | 1     |
| `fetch_stage`            | a02_fetch.py                 | 8     |
| `html_parse`             | a03_parse_html.py            | 12    |
| `docx_parse`             | a04_parse_docx.py            | 6     |
| `xlsx_parse`             | a05_parse_xlsx.py            | 6     |
| `pdf_parse`              | a06_parse_pdf.py             | 3     |
| `enrich_stage`           | a07_enrich.py                | 10    |
| `context_stage`          | a08_context.py               | 12    |
| `validate_stage`         | a09_validate.py              | 6     |
| `narrative_stage`        | a17_narrative.py             | 2     |
| `graph_synthesis_stage`  | a16_graph_synthesis.py       | 8     |
| `write_neo4j`            | a10_write_neo4j.py           | 14    |
| `write_postgres`         | a11_write_postgres.py        | 10    |
| `write_mongo`            | a12_write_mongo.py           | 3     |
| `write_redis`            | a13_write_redis.py           | 3     |
| `link_stage`             | a14_links.py                 | 8     |
| `completion_check`       | a15_control.py               | 3     |
| `final_stage`            | a15_control.py               | 2     |
| **Total**                |                              | **122** |
