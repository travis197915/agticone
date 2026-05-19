# Graph Report - /opt/clients/toystack/uhc/agentic_backend/uhc-backend-v2  (2026-05-17)

## Corpus Check
- Corpus is ~16,229 words - fits in a single context window. You may not need a graph.

## Summary
- 485 nodes · 691 edges · 72 communities (29 shown, 43 thin omitted)
- Extraction: 90% EXTRACTED · 10% INFERRED · 0% AMBIGUOUS · INFERRED: 71 edges (avg confidence: 0.77)
- Token cost: 6,800 input · 2,100 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Django REST API & Admin|Django REST API & Admin]]
- [[_COMMUNITY_Intake & Job Initialization|Intake & Job Initialization]]
- [[_COMMUNITY_HTML Document Parser|HTML Document Parser]]
- [[_COMMUNITY_Pipeline Control & Cleanup|Pipeline Control & Cleanup]]
- [[_COMMUNITY_Healthcare Code Detection|Healthcare Code Detection]]
- [[_COMMUNITY_Redis Job State Management|Redis Job State Management]]
- [[_COMMUNITY_BFS URL Fetch & Dedup|BFS URL Fetch & Dedup]]
- [[_COMMUNITY_Neo4j Graph Writers|Neo4j Graph Writers]]
- [[_COMMUNITY_HTTP Fetch & Content Detection|HTTP Fetch & Content Detection]]
- [[_COMMUNITY_Context Code Enrichment|Context Code Enrichment]]
- [[_COMMUNITY_LLM Semantic Enrichment|LLM Semantic Enrichment]]
- [[_COMMUNITY_Step & Decision Parsing|Step & Decision Parsing]]
- [[_COMMUNITY_XLSX Parser (AST)|XLSX Parser (AST)]]
- [[_COMMUNITY_XLSX Parser (Semantic)|XLSX Parser (Semantic)]]
- [[_COMMUNITY_PostgreSQL Writers|PostgreSQL Writers]]
- [[_COMMUNITY_Project Notes & Meta|Project Notes & Meta]]
- [[_COMMUNITY_DOCX Document Parser|DOCX Document Parser]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]

## God Nodes (most connected - your core abstractions)
1. `IngestionJob` - 20 edges
2. `_driver()` - 15 edges
3. `_run()` - 14 edges
4. `SopIngestionPipeline` - 13 edges
5. `_llm_json()` - 13 edges
6. `IngestionJobSerializer` - 13 edges
7. `_all_text()` - 12 edges
8. `_soup()` - 12 edges
9. `IngestedDocument` - 12 edges
10. `_exec()` - 11 edges

## Surprising Connections (you probably didn't know these)
- `SopIngestionPipeline` --uses--> `PipelineConfig`  [INFERRED]
  uhc-sop-ingestion/src/uhc_sop_ingestion/pipeline.py → uhc-sop-ingestion/src/uhc_sop_ingestion/config.py
- `HealthView` --uses--> `SopIngestionPipeline`  [INFERRED]
  sop_ingestion/views.py → uhc-sop-ingestion/src/uhc_sop_ingestion/pipeline.py
- `JobListCreateView` --uses--> `SopIngestionPipeline`  [INFERRED]
  sop_ingestion/views.py → uhc-sop-ingestion/src/uhc_sop_ingestion/pipeline.py
- `JobDetailView` --uses--> `SopIngestionPipeline`  [INFERRED]
  sop_ingestion/views.py → uhc-sop-ingestion/src/uhc_sop_ingestion/pipeline.py
- `SyncRunView` --uses--> `SopIngestionPipeline`  [INFERRED]
  sop_ingestion/views.py → uhc-sop-ingestion/src/uhc_sop_ingestion/pipeline.py

## Hyperedges (group relationships)
- **BFS Document Processing Loop** — a02_fetch_next_url_picker, a15_control_completion_checker, a15_control_state_clearer, a14_links_html_link_queue, a14_links_docx_link_queue, graph_route_after_pick, graph_route_completion [EXTRACTED 0.95]
- **Multi-Store Document Persistence Pattern** — a10_write_neo4j_god_node_writer, a11_write_postgres_pg_document_writer, a12_write_mongo_mongo_raw_writer, a12_write_mongo_mongo_parsed_writer [INFERRED 0.95]
- **Layered Document Format Detection (MIME → Extension → Magic Bytes)** — a02_fetch_content_type_detector, a02_fetch_extension_detector, a02_fetch_magic_bytes_detector [EXTRACTED 0.95]
- **XLSX Multi-Agent Parse Pipeline (6 cooperative agents over shared PipelineState)** — a05_parse_xlsx_xlsx_workbook_type, a05_parse_xlsx_xlsx_sheet_parser, a05_parse_xlsx_xlsx_code_extractor, a05_parse_xlsx_xlsx_calculator, a05_parse_xlsx_xlsx_metadata, a05_parse_xlsx_xlsx_header_detector [EXTRACTED 1.00]
- **Redis Job State Management Triad (cache + queue + progress tracking)** — a13_write_redis_redis_cache_writer, a13_write_redis_redis_queue_manager, a13_write_redis_redis_progress_tracker [EXTRACTED 1.00]
- **Django REST Ingestion API (views + serializers + URL routing)** — sop_ingestion_views_joblistcreateview, sop_ingestion_views_jobdetailview, sop_ingestion_views_healthview, sop_ingestion_views_syncrunview, sop_ingestion_serializers_ingestionjobserializer, sop_ingestion_urls_urlpatterns [INFERRED 0.95]

## Communities (72 total, 43 thin omitted)

### Community 0 - "Django REST API & Admin"
Cohesion: 0.09
Nodes (27): APIView, sop_backend root URL configuration, IngestedDocumentAdmin, IngestedDocumentInline, IngestionJobAdmin, 0001_initial migration (creates IngestionJob + IngestedDocument), IngestedDocument, IngestionJob (+19 more)

### Community 1 - "Intake & Job Initialization"
Cohesion: 0.05
Nodes (32): job_initializer(), mongo_job_logger(), INTAKE LAYER — 5 agents.  1. URLValidatorAgent      — checks the URL is non-empt, Validates seed_url is non-empty with a recognised scheme., Strips trailing whitespace, lowercases scheme, removes fragment., Creates a job_id and seeds the BFS queue with the root URL., Inserts the job document into MongoDB for durable audit logging., url_normalizer() (+24 more)

### Community 2 - "HTML Document Parser"
Cohesion: 0.12
Nodes (34): _codes(), _extract_anns(), _extract_group_rows(), _guess_decision(), html_annotations(), html_biz_table(), html_compound_tables(), html_decision_tables() (+26 more)

### Community 3 - "Pipeline Control & Cleanup"
Cohesion: 0.07
Nodes (23): completion_checker(), error_handler(), job_closer(), CONTROL LAYER — 5 agents.  1. CompletionCheckerAgent  — decides continue vs done, Marks job COMPLETED in Redis, MongoDB, and Postgres., Returns processing_complete=True when the BFS queue is exhausted., Resets all per-document fields so next iteration starts clean., Logs accumulated errors to Redis and marks job PARTIAL if any. (+15 more)

### Community 4 - "Healthcare Code Detection"
Cohesion: 0.07
Nodes (32): mongo_job_logger — MongoJobLoggerAgent, redis_job_tracker — RedisJobTrackerAgent, code_deduplicator — CodeDeduplicatorAgent, cpt_code_detector — CPTCodeDetectorAgent, denial_code_detector — DenialCodeDetectorAgent, eob_code_detector — EOBCodeDetectorAgent, ex_code_detector — EXCodeDetectorAgent, pos_code_detector — POSCodeDetectorAgent (+24 more)

### Community 5 - "Redis Job State Management"
Cohesion: 0.1
Nodes (25): Registers the job in Redis as a HASH for live progress tracking., redis_job_tracker(), _r(), REDIS LAYER — 3 agents.  1. RedisCacheWriterAgent    — caches parsed document JS, Caches the parsed document summary (without raw bytes) for 1 hour., Persists current BFS queue to Redis for crash recovery., Updates the live job progress hash in Redis., redis_cache_writer() (+17 more)

### Community 6 - "BFS URL Fetch & Dedup"
Cohesion: 0.1
Nodes (20): job_initializer — JobInitializerAgent, content_hasher — ContentHasherAgent (SHA256[:16]), duplicate_checker — DuplicateCheckerAgent, next_url_picker — NextURLPickerAgent (BFS pop), docx_link_queue — DOCXLinkQueueAgent (BFS enqueue), html_link_queue — HTMLLinkQueueAgent (BFS enqueue), pdf_link_queue — PDFLinkQueueAgent (BFS enqueue), xlsx_link_queue — XLSXLinkQueueAgent (BFS enqueue) (+12 more)

### Community 7 - "Neo4j Graph Writers"
Cohesion: 0.22
Nodes (20): annotation_node_writer(), branch_edge_writer(), child_doc_edge_writer(), code_node_writer(), _driver(), god_node_writer(), group_rule_node_writer(), group_rule_step_edge_writer() (+12 more)

### Community 8 - "HTTP Fetch & Content Detection"
Cohesion: 0.1
Nodes (19): content_hasher(), content_type_detector(), depth_limit_checker(), duplicate_checker(), extension_detector(), http_fetcher(), local_file_fetcher(), magic_bytes_detector() (+11 more)

### Community 9 - "Context Code Enrichment"
Cohesion: 0.22
Nodes (18): _add_code(), _all_text(), bill_type_detector(), code_deduplicator(), cpt_code_detector(), denial_code_detector(), entity_list_ref_detector(), eob_code_detector() (+10 more)

### Community 10 - "LLM Semantic Enrichment"
Cohesion: 0.18
Nodes (17): ambiguous_term_resolver(), cross_reference_resolver(), date_condition_nlp(), decision_row_classifier(), group_rule_nlp(), _llm_json(), potf_validator(), pre_section_rule_extractor() (+9 more)

### Community 11 - "Step & Decision Parsing"
Cohesion: 0.14
Nodes (15): html_compound_tables — HTMLCompoundTableAgent, html_decision_tables — HTMLDecisionTableAgent, html_steps — HTMLStepAgent, ambiguous_term_resolver — AmbiguousTermResolverAgent, cross_reference_resolver — CrossReferenceResolverAgent, date_condition_nlp — DateConditionNLPAgent, decision_row_classifier — DecisionRowClassifierAgent, group_rule_nlp — GroupRuleNLPAgent (+7 more)

### Community 12 - "XLSX Parser (AST)"
Cohesion: 0.27
Nodes (13): _find_col(), _header_row_idx(), _infer_code_system(), XLSX PARSE LAYER — 6 agents.  1. XLSXWorkbookTypeAgent   — CODE_TABLE | CALCULAT, Validates header detection — logs warning if first sheet has no clear header., Parses all sheets and stores them in reference_tables., _wb(), xlsx_calculator() (+5 more)

### Community 13 - "XLSX Parser (Semantic)"
Cohesion: 0.21
Nodes (14): _find_col (column finder by candidate names), _header_row_idx (header row detector), _infer_code_system (header-to-code-system mapper), _wb (XLSX workbook loader helper), xlsx_calculator (XLSXCalculatorAgent), xlsx_code_extractor (XLSXCodeExtractorAgent), xlsx_header_detector (XLSXHeaderDetectorAgent), xlsx_metadata (XLSXMetadataAgent) (+6 more)

### Community 14 - "PostgreSQL Writers"
Cohesion: 0.28
Nodes (12): _conn(), _exec(), pg_code_writer(), pg_date_condition_writer(), pg_document_writer(), pg_group_rule_writer(), pg_job_updater(), pg_link_writer() (+4 more)

### Community 15 - "Project Notes & Meta"
Cohesion: 0.18
Nodes (13): Anthropic Claude API, AST Extraction, coreBackend Project, graph.json Output, .graphify_analysis.json Output, Graphify Extract Command, Semantic Extraction via Claude, Celery (+5 more)

### Community 16 - "DOCX Document Parser"
Cohesion: 0.32
Nodes (11): _classify_table(), _doc(), docx_code_tables(), docx_headings(), docx_hyperlinks(), docx_metadata(), docx_paragraphs(), docx_tables() (+3 more)

### Community 17 - "Community 17"
Cohesion: 0.67
Nodes (4): _r (Redis connection factory helper), redis_cache_writer (RedisCacheWriterAgent), redis_progress_tracker (RedisProgressTrackerAgent), redis_queue_manager (RedisQueueManagerAgent)

### Community 19 - "Community 19"
Cohesion: 0.67
Nodes (3): content_type_detector — ContentTypeDetectorAgent, extension_detector — ExtensionDetectorAgent, magic_bytes_detector — MagicBytesDetectorAgent

### Community 20 - "Community 20"
Cohesion: 0.67
Nodes (3): html_pre_sections — HTMLPreSectionAgent, docx_headings — DOCXHeadingAgent, docx_paragraphs — DOCXParagraphAgent

### Community 21 - "Community 21"
Cohesion: 0.67
Nodes (3): Celery app (sop_backend Celery instance), sop_backend Django settings module, sop_backend WSGI application entry point

## Knowledge Gaps
- **177 isolated node(s):** `Centralised configuration loaded from .env (KEY=VALUE format).  Resolution order`, `Load KEY=VALUE pairs from a .env file into os.environ.      If env_path is None,`, `Load .env then build config from environment variables.`, `LangGraph StateGraph — wires all 121 agent functions into one pipeline.  Graph s`, `Bind cfg into an agent function so LangGraph sees (state) -> dict.` (+172 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **43 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SopIngestionPipeline` connect `Django REST API & Admin` to `Intake & Job Initialization`, `Pipeline Control & Cleanup`?**
  _High betweenness centrality (0.119) - this node is a cross-community bridge._
- **Why does `PipelineConfig` connect `Pipeline Control & Cleanup` to `Django REST API & Admin`?**
  _High betweenness centrality (0.038) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `IngestionJob` (e.g. with `IngestedDocumentSerializer` and `Meta`) actually correct?**
  _`IngestionJob` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 7 inferred relationships involving `SopIngestionPipeline` (e.g. with `PipelineConfig` and `HealthView`) actually correct?**
  _`SopIngestionPipeline` has 7 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Centralised configuration loaded from .env (KEY=VALUE format).  Resolution order`, `Load KEY=VALUE pairs from a .env file into os.environ.      If env_path is None,`, `Load .env then build config from environment variables.` to the rest of the system?**
  _177 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Django REST API & Admin` be split into smaller, more focused modules?**
  _Cohesion score 0.09 - nodes in this community are weakly interconnected._
- **Should `Intake & Job Initialization` be split into smaller, more focused modules?**
  _Cohesion score 0.05 - nodes in this community are weakly interconnected._