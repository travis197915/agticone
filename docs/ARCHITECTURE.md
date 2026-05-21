# Architecture

Wipro UHC Agentic Claims-Audit Platform — backend reference.

This document describes the system from the outside in: what the components are, how they talk to each other, and where state lives.

---

## 1. System diagram

```
                        ┌───────────────────────────────────────────────┐
                        │           React SPA (claims-frontend)         │
                        │   xyflow canvas · graph viewer · rule picker  │
                        └────────────┬──────────────────────┬───────────┘
                                     │                      │
                          /api/auth/  (JWT)         /api/builder, /api/ingest
                                     │                      │
                        ┌────────────▼───────────┐ ┌────────▼────────────────┐
                        │  claims-corebackend    │ │   sop_backend (Django)  │
                        │       (Node)           │ │                         │
                        │  • mints HS256 JWTs    │ │  ┌──────────────────┐   │
                        └────────────────────────┘ │  │ builder app      │   │
                                                   │  │  workflows, palette│
                                                   │  └──────────────────┘   │
                                                   │  ┌──────────────────┐   │
                                                   │  │ sop_ingestion app│   │
                                                   │  │  job rows, REST  │   │
                                                   │  └──────────────────┘   │
                                                   │           │             │
                                                   │  ┌────────▼─────────┐   │
                                                   │  │ Celery worker     │   │
                                                   │  │  runs the 122-    │   │
                                                   │  │  agent LangGraph  │   │
                                                   │  └────────┬─────────┘   │
                                                   └───────────┼─────────────┘
                                                               │
                       ┌───────────────────────────────────────┼───────────────────┐
                       │                                       │                   │
                  ┌────▼────┐  ┌─────────┐  ┌──────────┐  ┌────▼─────┐   ┌────────▼─────┐
                  │Postgres │  │ Neo4j   │  │ MongoDB  │  │  Redis   │   │  RabbitMQ    │
                  │ (canon) │  │ (graph) │  │ (raw +   │  │ (queue + │   │  (Celery     │
                  │         │  │         │  │  parsed) │  │  cache)  │   │   broker)    │
                  └─────────┘  └─────────┘  └──────────┘  └──────────┘   └──────────────┘
```

---

## 2. Components

### 2.1 Django project — `sop_backend/`

The Django project. Two installed apps:

| App              | Mount point      | Responsibility                                        |
|------------------|------------------|-------------------------------------------------------|
| `builder`        | `/api/builder/`  | Workflow CRUD, server-driven palette / nav / dashboard, atomic canvas save, per-node attachments. |
| `sop_ingestion`  | `/api/ingest/`   | Ingestion job rows, REST trigger, real-time stage/LLM logs, structured-SOP read APIs, HTML viewer. |

Settings live in [sop_backend/settings.py](../sop_backend/settings.py). Routing root in [sop_backend/urls.py](../sop_backend/urls.py).

### 2.2 Ingestion pipeline package — `uhc-sop-ingestion/`

A standalone Python package (installable via `pip install -e .`) that exposes `SopIngestionPipeline.run()`. Internally a LangGraph state machine with 17 stages and 122 agent functions.

Entry point: [uhc-sop-ingestion/src/uhc_sop_ingestion/pipeline.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/pipeline.py). Graph wiring: [graph.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/graph.py). State schema: [state.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/state.py).

See [PIPELINE.md](PIPELINE.md) and [AGENTS.md](AGENTS.md).

### 2.3 Runtime API-agent package — `uhc-api-agent/`

Second standalone package. Lets a workflow node execute an authenticated HTTP call at claim-evaluation time. Five-agent linear LangGraph: validate URL → resolve auth → call → parse JSON → log. Persists endpoint definitions and call history in Postgres + Mongo.

Entry point: [pipeline.py](../uhc-api-agent/src/uhc_api_agent/pipeline.py). See [RUNTIME_AGENT.md](RUNTIME_AGENT.md).

### 2.4 Worker — Celery

A single Celery app (`sop_backend.celery`) auto-discovers tasks. Two tasks live in `sop_ingestion.tasks`:

* `run_ingestion_pipeline(job_id)` — runs the 122-agent pipeline.
* `run_narrative_contextualizer(job_id)` — re-runs only the narrative stage on already-ingested SOPs.

Broker = RabbitMQ. Result backend = Redis.

---

## 3. Datastores

Each store has a single, well-defined role.

| Store      | Role                                                                 | Schema                                            |
|------------|----------------------------------------------------------------------|---------------------------------------------------|
| Postgres   | **Canonical**. Workflow state + every SOP entity + materialised graph + real-time stage/LLM logs. | Django models — see [DATA_MODEL.md](DATA_MODEL.md). |
| Neo4j      | **Graph traversal**. Same SOP graph re-expressed as `(:Document)-[:HAS_STEP]->(:Step)-[:HAS_DECISION]->(:Decision)…` for Cypher queries. | Written by `a10_write_neo4j` agents. |
| MongoDB    | **Append-only log**. Per-job raw bytes, parsed JSON, job progress documents for debugging / replay. | Three collections: `ingestion_jobs`, `raw_documents`, `parsed_documents`. |
| Redis      | **Hot state**. BFS queue persistence, agentic-graph shared context (`sop:graph:<job>:<section>`), live job progress hash, cross-agent intermediate JSON. | Plain keys + hashes. 7-day TTL on most. |
| RabbitMQ   | **Celery transport** only. Not user-facing.                          | n/a                                               |

Crucially, the **SPA never queries Neo4j directly**. Postgres's `AuditGraphNode` / `AuditGraphEdge` tables are the single source of truth that both the React-Flow viewer and the Cytoscape HTML viewer hit.

---

## 4. Authentication

JWTs are minted by the **Node `claims-corebackend`** service, not by Django. Django simply verifies the HS256 signature against a shared `JWT_SECRET`.

* Bridge: [builder/auth.py](../builder/auth.py) — `CorebackendJWTAuthentication`.
* Token payload: `{ "sub": "<user-id>", "email": "...", "role": "ADMIN"|"MEMBER", ... }`.
* DRF default: every endpoint requires `IsAuthenticated` except those that explicitly set `permission_classes = [AllowAny]` (the health check, the SOP graph/sections JSON endpoints, the sync-run dev endpoint, the contextualize endpoint, and the HTML viewer).

The bridge exposes a `CorebackendUser` dataclass on `request.user`; `request.user.role` drives RBAC (e.g. `NavItem.min_role = ADMIN` filters the sidebar for non-admins).

---

## 5. Request flows

### 5.1 Create workflow + attach SOP

```
SPA                          Django (builder)              Celery
 │  POST /api/builder/         │                              │
 │  workflows/ {sop_urls:[…]}  │                              │
 ├──────────────────────────► │                              │
 │                             │ Workflow row                 │
 │                             │ + IngestionJob(s)            │
 │                             │ + workflow.metadata          │
 │                             │   .runtime_agents (if any)   │
 │                             │                              │
 │                             │ run_ingestion_pipeline.delay │
 │                             ├─────────────────────────────►│
 │  202 Accepted               │                              │ 122-agent
 │  (with job_id list)         │                              │ LangGraph
 │◄────────────────────────── │                              │ (60–180s)
 │                             │                              │
 │  GET /api/ingest/<job>/     │                              │
 │  (poll until COMPLETED)     │                              │
 ├──────────────────────────► │                              │
```

### 5.2 Ingestion pipeline (one document)

```
intake_stage → pick_next_url ── (queue empty) ──► final_stage ► END
                    │
                    └─► fetch_stage
                            │
                            ├─ duplicate ─► link_stage ─► completion_check ─┐
                            │                                                │
                            └─ new doc                                       │
                                  │                                          │
                       html_parse / docx_parse / xlsx_parse / pdf_parse      │
                                  │                                          │
                              enrich_stage      (10 LLM agents)              │
                                  │                                          │
                              context_stage     (12 code extractors)         │
                                  │                                          │
                              validate_stage    (6 sanity checks)            │
                                  │                                          │
                              narrative_stage   (2 LLM narrators)            │
                                  │                                          │
                          graph_synthesis_stage (8 LLM agents → graph)       │
                                  │                                          │
                              write_neo4j       (14 writers)                 │
                                  │                                          │
                              write_postgres    (10 writers)                 │
                                  │                                          │
                              write_mongo       (3 writers)                  │
                                  │                                          │
                              write_redis       (3 writers)                  │
                                  │                                          │
                              link_stage        (8 link agents)              │
                                  │                                          │
                                  └──────────────────────────────────────────┘
                                                completion_check ─► loop or final
```

Every node is wrapped by `_stage()` in [graph.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/graph.py), which writes a `PipelineStageLog` row in Postgres on entry and exit (real-time, via a dedicated psycopg2 autocommit connection in [pg_logger.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/pg_logger.py)).

### 5.3 Per-node rule attachment

```
SPA               Django (builder)             Postgres
 │ GET /workflows/  │                            │
 │   <id>/attachable│                            │
 ├────────────────► │                            │
 │                  │ JOIN IngestionJob          │
 │                  │  → AuditSop                │
 │                  │  → AuditPrecondition       │
 │                  │  → AuditStep + decisions   │
 │                  │ Plus workflow.metadata     │
 │                  │  .runtime_agents           │
 │                  │◄───────────────────────────│
 │  {sop_rules:[…], │                            │
 │   tool_calls:[…]}│                            │
 │◄──────────────── │                            │
 │                  │                            │
 │ User picks rules │                            │
 │ in dialog.       │                            │
 │ PUT /workflows/  │                            │
 │   <id>/graph/    │                            │
 │  (full canvas    │                            │
 │   incl. each     │                            │
 │   Shape's        │                            │
 │   properties.    │                            │
 │   sop_rules)     │                            │
 ├────────────────► │                            │
```

Rules are stored on `Shape.properties.sop_rules` (a JSONB array of keys like `step:<sop>:<step#>:<row#>`).

---

## 6. Code organization

```
uhc-agentic-backend/
├── manage.py                      Django entrypoint
├── requirements.txt               Top-level deps + editable installs
├── .env                           Hosted-service credentials (see SETUP.md)
│
├── sop_backend/                   Django project
│   ├── settings.py                ENV-driven config
│   ├── urls.py                    Root router → builder + sop_ingestion
│   ├── celery.py                  Celery app factory
│   └── wsgi.py
│
├── builder/                       Workflow builder app
│   ├── models.py                  Workflow → WorkArea → Workbench → Shape
│   ├── catalog_seed.py            Server-driven palette / nav / widgets
│   ├── attachments.py             SOP + runtime-agent attach side-effects
│   ├── auth.py                    JWT bridge to claims-corebackend
│   ├── services.py                WorkflowGraphWriter — atomic canvas save
│   ├── views.py / serializers.py
│   ├── management/commands/seed_builder_catalog.py
│   └── urls.py
│
├── sop_ingestion/                 Ingestion REST surface
│   ├── models.py                  IngestionJob, Audit* tables, graph tables
│   ├── tasks.py                   Celery: run_pipeline + contextualize
│   ├── services/contextualizer.py Narrative backfill
│   ├── views.py                   REST endpoints + HTML viewer
│   ├── serializers.py
│   └── urls.py
│
├── uhc-sop-ingestion/             Editable package — the 122-agent pipeline
│   └── src/uhc_sop_ingestion/
│       ├── pipeline.py            SopIngestionPipeline class
│       ├── graph.py               LangGraph wiring (17 stages)
│       ├── state.py               PipelineState TypedDict
│       ├── config.py              PipelineConfig.from_env() + DB getters
│       ├── pg_logger.py           Real-time Postgres stage/LLM logger
│       └── agents/
│           ├── a01_intake.py          (5 agents)
│           ├── a02_fetch.py           (9 agents)
│           ├── a03_parse_html.py      (12 agents)
│           ├── a04_parse_docx.py      (6 agents)
│           ├── a05_parse_xlsx.py      (6 agents)
│           ├── a06_parse_pdf.py       (3 agents)
│           ├── a07_enrich.py          (10 LLM agents)
│           ├── a08_context.py         (12 code detectors)
│           ├── a09_validate.py        (6 validators)
│           ├── a10_write_neo4j.py     (14 writers)
│           ├── a11_write_postgres.py  (10 writers)
│           ├── a12_write_mongo.py     (3 writers)
│           ├── a13_write_redis.py     (3 writers)
│           ├── a14_links.py           (8 link routers)
│           ├── a15_control.py         (5 control)
│           ├── a16_graph_synthesis.py (8 LLM agents)
│           └── a17_narrative.py       (2 narrators)
│
└── uhc-api-agent/                 Editable package — runtime HTTP agent
    └── src/uhc_api_agent/
        ├── pipeline.py            ApiAgentPipeline class
        ├── graph.py               5-node LangGraph
        ├── state.py
        ├── store.py               CredentialStore (Postgres-backed)
        └── agents/                a01_validate_url … a05_log_response
```

---

## 7. LLM strategy

Two providers, chosen per call by the enrichment agent in [a07_enrich.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a07_enrich.py).

| Provider           | Used for                                                                 |
|--------------------|--------------------------------------------------------------------------|
| Anthropic Claude   | Reasoning, classification, free-text writing (decision-row classifier, narrative writers, semantic edge reasoner). |
| OpenAI GPT-4o      | Structured extraction with `response_format=json_object` (date conditions, group rules, pre-section rules, code extraction in `a08_context`). |

The single shared helper `_llm_call()` provides:

1. Retry × 2 with the **same** provider.
2. Cross-provider fallback if the primary fails the retries.
3. Schema validation (`_validate_schema`) — invalid output triggers retry.
4. One row inserted into `sop_ingestion_llmcalllog` for cost telemetry.

Every LLM call is bookkept; aggregates are written to `IngestionJob.total_llm_calls / total_tokens_in / total_tokens_out` at the end of the run.

---

## 8. Why this layout?

A few design choices that aren't obvious from the code.

* **Two graph stores (Postgres + Neo4j)**. Neo4j is great for traversal, but the SPA fetches the full subgraph for one SOP at a time — that's a single Postgres SELECT, no Bolt protocol needed. Neo4j is kept for ad-hoc Cypher exploration and future audit reasoners.
* **Materialised `AuditGraphNode` / `AuditGraphEdge` even though Postgres already has structured tables.** The structured tables (`AuditStep`, `AuditDecision`, …) are how a human auditor reads the SOP. The graph tables are how a downstream reasoner traverses it. Keeping both means the SPA never has to assemble a graph at request time.
* **Edges in `localStorage`, not Postgres.** Edge UX iterates fast (handle drag, waypoints, label nudging) and the bulk-save round-trip on every drag would be unusable. Once a workflow is *finalised*, edges are persisted alongside the canvas via `PUT /workflows/<id>/graph/`.
* **122 small agents instead of one big function.** The pipeline is fully observable: every stage writes a `PipelineStageLog` row with start/end timestamps and `duration_ms`. When something fails you can pinpoint which agent and on which document.
* **Hosted services for everything.** `.env` points at managed Postgres/Redis/Neo4j/Mongo/RabbitMQ instances on `toystack.store`. Local Docker compose is only needed if you want to develop offline — see [SETUP.md](SETUP.md).

---

## 9. Where to read next

* [SETUP.md](SETUP.md) — get a dev environment running.
* [API.md](API.md) — full endpoint reference with sample requests / responses.
* [openapi.yaml](openapi.yaml) — import into Postman.
* [DATA_MODEL.md](DATA_MODEL.md) — every Django model, every field.
* [PIPELINE.md](PIPELINE.md) — stage-by-stage walkthrough.
* [AGENTS.md](AGENTS.md) — per-agent reference (all 122).
* [RUNTIME_AGENT.md](RUNTIME_AGENT.md) — the `uhc-api-agent` package.
* [STORAGE.md](STORAGE.md) — datastore conventions (existing doc).
