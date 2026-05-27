# Wipro UHC — Agentic Claims-Audit Platform

Server-driven workflow builder + SOP knowledge-graph ingestion + runtime API
agents.  Designed so a claims auditor (and the LLMs that assist them) can
ingest any HTML / DOCX / XLSX / PDF SOP, materialise it into a queryable
knowledge graph, and attach individual rules from that graph onto nodes in
a visual workflow.

---

## 1.  Architecture at a glance

```
                ┌──────────────────────────────────────────┐
                │           React SPA (claims-frontend)    │
                │  • Workflow builder canvas (xyflow)      │
                │  • SOP graph viewer + sections panel     │
                │  • Per-node rule / tool-call attachments │
                └─────────────┬───────────────────┬────────┘
                              │                   │
                  REST /api/auth/        REST /api/builder/, /api/ingest/
                              │                   │
                ┌─────────────▼────┐    ┌─────────▼─────────────────┐
                │ Node identity    │    │   Django (uhc-backend-v2) │
                │ (claims-corebkd) │    │                           │
                │   • JWT issuer   │    │ ┌─ builder/   (this repo) │
                └──────────────────┘    │ │   Workflows / shapes /  │
                                        │ │   server-driven palette │
                                        │ ├─ sop_ingestion/         │
                                        │ │   Job rows, audit tables│
                                        │ │   + sections / graph    │
                                        │ │   REST endpoints        │
                                        │ ├─ uhc-sop-ingestion/     │
                                        │ │   122-agent LangGraph   │
                                        │ │   pipeline (sub-pkg)    │
                                        │ └─ uhc-api-agent/         │
                                        │     Runtime HTTP agents   │
                                        └─────┬─────────┬───────────┘
                                              │         │
                                  Postgres ◄──┘         └──► Neo4j (graph)
                                  Redis (job cache + queue)
                                  MongoDB (raw + parsed snapshots)
```

* **Single Postgres** holds canonical workflow state (`builder_*` tables)
  and the full SOP record (`sop_ingestion_*` audit tables + materialised
  graph nodes/edges).
* **Neo4j** holds the canonical knowledge graph for traversal queries;
  Postgres carries the same graph in `AuditGraphNode` / `AuditGraphEdge`
  so the SPA never has to hit Neo4j directly.
* **Redis** is shared cache between the ingestion agents (queue + LLM
  intermediate state).
* **MongoDB** is an append-only log of raw + parsed documents per job for
  debugging and replay.

---

## 2.  Repository layout

```
uhc-backend-v2/
├── sop_backend/                 Django project settings + URL root
├── builder/                     The workflow builder app
│   ├── models.py                Workflow → WorkArea → Workbench → Shape
│   ├── catalog_seed.py          Server-driven palette / nav / dashboard
│   ├── attachments.py           SOP + runtime-agent attach side-effects
│   ├── views.py / serializers.py
│   └── urls.py                  /api/builder/
├── sop_ingestion/               REST surface + Celery glue
│   ├── models.py                AuditSop, AuditStep, AuditDecision, …
│   ├── tasks.py                 Celery: run_pipeline + contextualize_job
│   ├── services/contextualizer.py    Narrative backfill for old SOPs
│   ├── views.py / urls.py       /api/ingest/
│   └── migrations/
├── uhc-sop-ingestion/           The 122-agent LangGraph package
│   └── src/uhc_sop_ingestion/
│       ├── pipeline.py          SopIngestionPipeline.run()
│       ├── graph.py             LangGraph wiring (16 stages)
│       └── agents/
│           ├── a01_intake.py             (5  agents)
│           ├── a02_fetch.py              (9  agents)
│           ├── a03_parse_html.py         (12 agents)
│           ├── a04_parse_docx.py         (6  agents)
│           ├── a05_parse_xlsx.py         (6  agents)
│           ├── a06_parse_pdf.py          (3  agents)
│           ├── a07_enrich.py             (10 agents)
│           ├── a08_context.py            (12 agents)
│           ├── a09_validate.py           (6  agents)
│           ├── a10_write_neo4j.py        (14 agents)
│           ├── a11_write_postgres.py     (10 agents)
│           ├── a12_write_mongo.py        (3  agents)
│           ├── a13_write_redis.py        (3  agents)
│           ├── a14_links.py              (8  agents)
│           ├── a15_control.py            (5  agents)
│           ├── a16_graph_synthesis.py    (8  agents)
│           └── a17_narrative.py          (2  agents)
├── uhc-api-agent/               Runtime HTTP-call package (LangGraph)
├── requirements.txt
└── manage.py
```

**Total ingestion agents: 122 functions across 17 modules / 16 LangGraph stages.**

---

## 3.  The ingestion pipeline (uhc-sop-ingestion)

LangGraph state machine.  Every node is one of the 122 functions above,
wrapped in `_stage()` with Postgres real-time logging.

```
START
 │
 [intake_stage]  url_validator → url_normalizer → job_initializer
                 → redis_job_tracker → mongo_job_logger
 │
 ┌─────────────── BFS LOOP ───────────────────────────────────────────┐
 │ pick_next_url                                                       │
 │   ├── no more URLs   → final_stage                                  │
 │   └── url available                                                 │
 │        ↓                                                            │
 │   fetch_stage   (9 fetch / sniff / hash / dup-check agents)         │
 │     ├── duplicate?  → link_stage (back to loop)                     │
 │     └── new doc                                                     │
 │          ↓ route by format                                          │
 │   html_parse | docx_parse | xlsx_parse | pdf_parse                  │
 │          ↓                                                          │
 │   enrich_stage          (10 LLM agents — Claude + GPT-4o)           │
 │          ↓                                                          │
 │   context_stage         (12 code-detection agents)                  │
 │          ↓                                                          │
 │   validate_stage        (6 sanity checks)                           │
 │          ↓                                                          │
 │   narrative_stage       (sop_overview + per-step narrator)          │
 │          ↓                                                          │
 │   graph_synthesis_stage (8 LLM agents → unified graph)              │
 │          ↓                                                          │
 │   write_neo4j → write_postgres → write_mongo → write_redis          │
 │          ↓                                                          │
 │   link_stage  (extracts referenced URLs, enqueues for next loop)    │
 │          ↓                                                          │
 │   completion_check ─── more work? ─────────── pick_next_url ────────┘
 │
 final_stage → END
```

### 3.1  Per-stage breakdown

| # | Stage                    | Agents | Purpose                                                            |
|---|--------------------------|-------:|--------------------------------------------------------------------|
| 1 | `intake_stage`           | 5 | Validate / normalise URL, register job in Postgres / Redis / Mongo |
| 2 | `pick_next_url`          | 1 | BFS pop from queue                                                  |
| 3 | `fetch_stage`            | 9 | HTTP / file fetch + magic-byte sniffing + dedup                     |
| 4 | `html_parse`             | 12 | Tables, pre-sections, steps, decisions, annotations, links         |
| 4 | `docx_parse`             | 6 | Headings / paragraphs / tables / hyperlinks                         |
| 4 | `xlsx_parse`             | 6 | Workbook type / sheet rows / code extraction                        |
| 4 | `pdf_parse`              | 3 | Text extraction + metadata + normalisation                          |
| 5 | `enrich_stage`           | 10 | LLM: refine step Qs, classify decisions, semantic-enrich rules     |
| 6 | `context_stage`          | 12 | EOB / EX / DENIAL / POS / REVENUE / BILL / MOD / FREQ / CPT codes  |
| 7 | `validate_stage`         | 6 | Doc completeness, step sequence, decision-row, code-system checks  |
| 8 | `narrative_stage` **NEW**| 2 | SOP-level narrative + per-step narrative paragraphs                |
| 9 | `graph_synthesis_stage`  | 8 | LLM document profiler → unified knowledge graph                    |
| 10| `write_neo4j`            | 14 | God-node, pre-section, step, rule, code, annotation + edges       |
| 11| `write_postgres`         | 10 | AuditSop + all child rows + materialised graph                    |
| 12| `write_mongo`            | 3 | Raw + parsed snapshots + job progress                              |
| 13| `write_redis`            | 3 | Cache + queue + progress counters                                  |
| 14| `link_stage`             | 8 | Classify discovered links + enqueue per-format                     |
| 15| `completion_check`       | 3 | Error handling + state clear + completion test                     |
| 16| `final_stage`            | 2 | Final summary + job closer                                         |

### 3.2  LLM provider strategy

* **Anthropic (Claude Sonnet 4.5)** — reasoning / classification / writing
  (decision-row classifier, narrative writers, semantic edge reasoner).
* **OpenAI (GPT-4o)** — structured extraction (date conditions, group
  rules, pre-section rules) — uses `response_format: json_object`.
* Every call passes through `_llm_call()` in `a07_enrich.py` which gives:
  retry ×2 → cross-provider fallback → schema validation → row in
  `LLMCallLog` for cost telemetry.

### 3.3  Narrative agent (NEW)

`a17_narrative.py` runs after structural extraction:

* `sop_overview_narrator` → 5-8 sentence executive narrative for the
  whole SOP (audience, walkthrough, notable codes).  Persisted to
  `AuditSop.narrative_context`.
* `step_narrative_writer` → 2-3 sentence paragraph per step explaining
  purpose, how the auditor walks the If/Then rows, and where the flow
  goes next.  Persisted to `AuditStep.narrative_context`.

Both reuse `_llm_call` so they inherit retries / fallback / logging.
A standalone backfill endpoint (`POST /api/ingest/<job_id>/contextualize/`)
re-runs them on already-ingested SOPs without re-fetching the source HTML.

---

## 4.  Workflow builder (`builder` app)

### 4.1  Hierarchy (database)

```
Workflow                       UUIDPK + metadata JSONB + owner
  └─ WorkArea                  swim-lane / phase
        └─ Workbench           sub-canvas inside a phase
              └─ Shape         one xyflow node
                  └─ properties JSONB  (carries attached SOP rules + tool calls)

ShapeConnection                directed edge between two Shapes
```

### 4.2  Server-driven palette — 8 canonical shapes

The frontend has zero hardcoded shape knowledge.  Every entry below is
rendered from `/api/builder/catalog/shapes/`:

| Slug                | Flowchart label       | Role                      | Colour palette (fill / stroke / text / accent) |
|---------------------|-----------------------|---------------------------|--------------------------------------------------|
| `round-rectangle`   | Terminator            | Start / End               | emerald (`#ecfdf5 / #10b981 / #065f46 / #10b981`) |
| `rectangle`         | Process               | Action                    | blue (`#eff6ff / #3b82f6 / #1e3a8a / #3b82f6`)   |
| `diamond`           | Decision              | Branch / Conditional      | amber (`#fffbeb / #f59e0b / #78350f / #f59e0b`)  |
| `parallelogram`     | Data                  | Input / Output            | violet (`#f5f3ff / #8b5cf6 / #4c1d95 / #8b5cf6`) |
| `hexagon`           | Preparation           | Setup / Init              | orange (`#fff7ed / #f97316 / #7c2d12 / #f97316`) |
| `cylinder`          | Database              | Persistent storage        | teal (`#f0fdfa / #14b8a6 / #134e4a / #14b8a6`)   |
| `circle`            | Connector             | Reference / Connector     | slate (`#f1f5f9 / #64748b / #1e293b / #64748b`)  |
| `arrow-rectangle`   | Predefined Process    | Subprocess                | indigo (`#eef2ff / #6366f1 / #312e81 / #6366f1`) |

Each shape has four ports (`top`, `right`, `bottom`, `left`) and a
`property_schema` driving the inspector form — adding a new field is a
catalog-only change, no frontend deploy needed.

### 4.3  Edges

Edges are a frontend-only concern stored in `localStorage` (Django's
`ShapeConnection` table is wiped on every graph save).  Reasons:

* Edge UX iterates much faster than node state.
* Removes mismatch between xyflow's edge model and the Django bulk-save.

Users can change the edge type at runtime (default / straight / step /
smoothstep) and label edges with arbitrary text plus quick "Yes" / "No"
buttons.

---

## 5.  Workflow attachments (per-workflow)

Two pieces of context attach to a workflow on create or via the
"+" buttons in the right pane:

### 5.1  SOP URLs → ingestion jobs

* User submits any HTML / DOCX / XLSX / PDF URL.
* Backend creates an `IngestionJob` FK'd to the workflow.
* Celery dispatches `run_ingestion_pipeline.delay(job_id)`.
* The 122-agent pipeline ingests, narrates, and persists the SOP.
* The workflow's context panel polls for status and unlocks the graph
  viewer + rule picker once `COMPLETED`.

### 5.2  Runtime API agents → endpoint configs

* Each agent is a callable HTTP endpoint (`url`, `method`, `auth`).
* Registered via `uhc-api-agent.ApiAgentPipeline.register()` which writes
  to `api_agent_endpoints` and returns an `endpoint_id`.
* `endpoint_id` is stored on `Workflow.metadata['runtime_agents'][i]`.
* At execution time the workflow can fan out HTTP calls per claim using
  these registered endpoints.

---

## 6.  Per-node attachments

The "Pick rules & tools" button on every node inspector opens a dialog
backed by `GET /api/builder/workflows/<id>/attachable/`.

### 6.1  SOP rules

Each rule line returned is one of:

* **Pre-condition rule** — key `pre:<sop>:<section>:<idx>`, from
  `AuditPrecondition.llm_rules`.
* **Decision row** — key `step:<sop>:<step#>:<row#>`, from
  `AuditStep.decisions`.

Each row carries:

* The condition / action / decision-type / codes,
* The parent section's **narrative** (so users see "why this rule"),
* `references[]` — the keys of all rows in any `goto_step` target
  (used for **cascade selection**: ticking a rule auto-ticks the whole
  step it jumps to).

Selected rules are saved on `Shape.properties.sop_rules`.

### 6.2  Tool calls

The same dialog has a **Tool Calls** tab listing all `runtime_agents`
attached to the workflow.  Selections are stored on
`Shape.properties.tool_calls`.

### 6.3  Multi-SOP picker

If a workflow has multiple SOPs attached, the picker shows a chip row
across the top (`All (N)` + one chip per SOP).  Rules are grouped by
**SOP → section → rule**, with the SOP's narrative as a one-line
preview banner above each group.

---

## 7.  REST API surface

### `/api/builder/`  (builder app)

| Method | Path                                | Purpose                                          |
|--------|-------------------------------------|--------------------------------------------------|
| GET    | `/catalog/categories/`              | Shape catalog grouped by category                |
| GET    | `/catalog/shapes/`                  | Flat shape list (palette items)                  |
| GET    | `/ui/navigation/`                   | Server-driven sidebar entries                    |
| GET    | `/ui/dashboard/`                    | Dashboard widget definitions                     |
| GET    | `/workflows/`                       | List workflows                                   |
| POST   | `/workflows/`                       | Create (`sop_urls`, `runtime_agents` accepted)   |
| GET    | `/workflows/<id>/`                  | Workflow detail (with attached SOPs + agents)    |
| PATCH  | `/workflows/<id>/`                  | Update name / description / active               |
| DELETE | `/workflows/<id>/`                  | Delete + cascade                                 |
| GET    | `/workflows/<id>/graph/`            | Full nested graph for the canvas                 |
| PUT    | `/workflows/<id>/graph/`            | Atomic save of the canvas                        |
| POST   | `/workflows/<id>/attach/`           | Attach more SOPs / agents to an existing workflow|
| GET    | `/workflows/<id>/attachable/`       | Rules + tools available for per-node attachment  |
| POST   | `/workflows/<id>/duplicate/`        | Clone the workflow                               |
| POST   | `/workflows/<id>/activate/`         | Mark active                                      |
| POST   | `/workflows/<id>/deactivate/`       | Mark inactive                                    |

### `/api/ingest/`  (sop_ingestion app)

| Method | Path                                  | Purpose                                  |
|--------|---------------------------------------|------------------------------------------|
| GET    | `/health/`                            | Package + Celery health check (no auth)  |
| POST   | `/`                                   | Start an async ingestion job             |
| GET    | `/`                                   | List jobs                                |
| GET    | `/<job_id>/`                          | Poll job status                          |
| DELETE | `/<job_id>/`                          | Delete a completed job                   |
| POST   | `/run-sync/`                          | Run pipeline inline (DEBUG only)         |
| GET    | `/<job_id>/graph/`                    | Knowledge graph (nodes + edges) as JSON  |
| GET    | `/<job_id>/sections/`                 | Structured sections + rules + narrative  |
| POST   | `/<job_id>/contextualize/`            | Re-run narrative agents on existing SOPs |
| GET    | `/viewer/`                            | HTML viewer — job list                   |
| GET    | `/viewer/<job_id>/`                   | HTML viewer — job detail                 |
| GET    | `/viewer/<job_id>/doc/<doc_id>/`      | HTML viewer — full SOP document          |

---

## 8.  Data model (Postgres)

### `builder_*`

* `builder_workflow`             — top-level container, `metadata` JSONB carries `runtime_agents`.
* `builder_work_area`            — phase / swim lane.
* `builder_workbench`            — sub-canvas inside a phase.
* `builder_shape`                — one xyflow node, `properties` JSONB carries `sop_rules` + `tool_calls`.
* `builder_shape_connection`     — directed edge (table kept for completeness; edges live in localStorage).
* `builder_shape_category`       — palette section ("General").
* `builder_shape_definition`     — one palette item (slug, kind, viewbox, ports, schema, default style).
* `builder_nav_item`             — sidebar entry.
* `builder_dashboard_widget`     — dashboard tile.

### `sop_ingestion_*`

* `ingestionjob`                 — root row, FK to `Workflow` (nullable).
* `ingesteddocument`             — per-document outcome inside a job.
* `pipelinestagelog`             — one row per LangGraph node execution (real-time).
* `llmcalllog`                   — one row per LLM API call (provider, model, tokens, ms, success).
* `auditsop`                     — root SOP record, `narrative_context` + `llm_summary`.
* `auditprecondition`            — pre-section rules (`llm_rules` JSONB).
* `auditstep`                    — decision point, `narrative_context` + `intro_text`.
* `auditdecision`                — one If/Then row, codes split by family + `all_codes`.
* `auditgrouplimit`              — group-specific timely-filing windows.
* `auditcode`                    — every claims code mentioned in the SOP.
* `auditdatecondition`           — date-range applicability rules.
* `auditannotation`              — embedded notes / alerts / exceptions.
* `auditreference`               — cross-references to other SOPs.
* `auditgraphnode` / `auditgraphedge` — materialised knowledge graph (mirror of Neo4j).

---

## 9.  Local setup

### 9.1  Services (Postgres / Redis / Neo4j / Mongo)

Pointed at via `.env`.  Production hostnames live there; for local you
can run them via Docker:

```bash
docker run -d --name pg     -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16
docker run -d --name redis  -p 6379:6379 redis:7
docker run -d --name neo4j  -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/test1234 neo4j:5
docker run -d --name mongo  -p 27017:27017 mongo:7
```

### 9.2  Python venv

```bash
python3.13 -m venv ../src
source ../src/bin/activate
pip install -r requirements.txt
pip install -e uhc-sop-ingestion
pip install -e uhc-api-agent
```

### 9.3  Django

```bash
PYTHONPATH=. python manage.py migrate
PYTHONPATH=. python manage.py seed_builder_catalog     # populates shapes/nav/widgets
PYTHONPATH=. python manage.py runserver 0.0.0.0:8000
```

### 9.4  Celery master worker

Ingestion uses a **master dispatcher** (`job_queue`): Celery spawns one OS subprocess
per `job_id`; LangGraph runs in `sop_ingestion.worker.job_runner`, not in the worker.

```bash
PYTHONPATH=. celery -A sop_backend worker -Q job_queue,celery --concurrency=1 -l INFO
```

Set parallel subprocess cap (default `10`):

```bash
export MAX_PIPELINE_SUBPROCESSES=10   # or 50 on a large host
```

### 9.5  Frontend

```bash
cd ../../frontend/claims-frontend
yarn install
yarn dev    # http://localhost:5173
```

---

## 10.  Common operations

### Trigger an ingestion

```bash
curl -X POST http://localhost:8000/api/ingest/ \
  -H 'Content-Type: application/json' \
  -d '{"seed_url": "http://localhost:9191/obh_facets_timely_filing.html"}'
```

Or from the UI: create a workflow with one or more SOP URLs.

### Backfill narratives on an existing SOP

```bash
# Async (returns 202 + celery_task_id)
curl -X POST http://localhost:8000/api/ingest/<job_id>/contextualize/

# Sync (blocks ~60s, returns the results)
curl -X POST 'http://localhost:8000/api/ingest/<job_id>/contextualize/?sync=true'
```

### Inspect the knowledge graph

* JSON:   `GET /api/ingest/<job_id>/graph/`
* HTML:   `GET /api/ingest/viewer/<job_id>/doc/<sop_id>/`
* In-app: open the workflow → click the SOP card in the right pane → the
          dialog shows React-Flow graph on the left and a sections table
          on the right (with collapsible pre-conditions, decision tree,
          codes, annotations, references).

### Reseed the palette

```bash
PYTHONPATH=. python manage.py seed_builder_catalog
```

This is idempotent and runs automatically post-migrate.

---

## 11.  Feature summary

| Feature                                       | Status |
|-----------------------------------------------|--------|
| Server-driven palette (8 shapes, 4 ports)     | ✅ |
| Server-driven sidebar nav                     | ✅ |
| Server-driven dashboard widgets               | ✅ |
| Workflow CRUD + duplicate + activate/deactivate | ✅ |
| Atomic graph save (`PUT /workflows/<id>/graph/`) | ✅ |
| Edge type picker (default / straight / step / smoothstep) | ✅ |
| Edge labels (free-text + quick Yes / No)      | ✅ |
| SOP ingestion via Celery + 122-agent LangGraph | ✅ |
| HTML / DOCX / XLSX / PDF parsers              | ✅ |
| LLM enrichment with dual-provider fallback    | ✅ |
| Code detection (EOB / EX / DENIAL / SYSTEM / POS / REV / BILL / MOD / FREQ / CPT) | ✅ |
| Neo4j + Postgres dual-write of the graph      | ✅ |
| Mongo raw + parsed snapshots                  | ✅ |
| Per-stage Postgres logging (real-time)        | ✅ |
| Per-LLM-call Postgres logging (tokens, ms)    | ✅ |
| SOP-level narrative agent                     | ✅ |
| Per-step narrative agent (batched)            | ✅ |
| Backfill endpoint for existing SOPs           | ✅ |
| Native React-Flow knowledge-graph viewer      | ✅ |
| Sections panel with rule tables               | ✅ |
| Per-node SOP-rule attachment                  | ✅ |
| Per-node tool-call attachment                 | ✅ |
| Multi-SOP rule picker with SOP filter         | ✅ |
| Cascade selection via `goto_step` references  | ✅ |
| "Why this rule?" narrative on every chip      | ✅ |
| Runtime API agents (`uhc-api-agent`)          | ✅ |
| JWT auth via `claims-corebackend` (Node)      | ✅ |

---

## 12.  Glossary

| Term                | Meaning                                                                        |
|---------------------|--------------------------------------------------------------------------------|
| **SOP**             | Standard Operating Procedure (the claims policy document being ingested).      |
| **Pre-condition**   | Section the auditor checks *before* entering the decision tree.                |
| **Step**            | One decision point in the audit tree.                                          |
| **Decision row**    | One If / And / Then row inside a step.                                         |
| **Workflow**        | The top-level visual canvas representing an audit procedure.                   |
| **Work area**       | A swim-lane / phase inside a workflow.                                         |
| **Workbench**       | A sub-canvas inside a work area.                                               |
| **Shape**           | One node on the canvas (instance of a `ShapeDefinition`).                      |
| **Catalog**         | Server-driven set of `ShapeDefinition`, `NavItem`, `DashboardWidget` rows.     |
| **Runtime agent**   | A registered HTTP endpoint the workflow calls per-claim at execution time.     |
| **Narrative**       | LLM-generated story-style paragraph describing a step / SOP in plain English.  |
| **Cascade select**  | Picking a rule auto-picks every rule it transitively references via `goto_step`.|
