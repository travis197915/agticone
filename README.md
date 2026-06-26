# Wipro UHC — Agentic Claims-Audit Platform

A Django backend that powers an end-to-end **claims-audit** workflow system: it
ingests Standard Operating Procedure (SOP) documents into a structured,
queryable knowledge base, lets auditors compose audit workflows on a drag-drop
canvas, and runs live claims through those workflows with LLM-driven rule
evaluation and registered API tools.

---

## 1. What it does

The platform has four capabilities, each backed by a Django app and/or an
installable pipeline package:

1. **SOP Ingestion** — a LangGraph pipeline crawls HTML / DOCX / XLSX / PDF
   SOPs, parses their structure, enriches them with LLMs, synthesises a
   Pydantic **Intermediate Representation (IR)**, and persists to Postgres +
   Neo4j + MongoDB + Redis.
2. **Workflow Builder** — a REST API for a server-driven drag-drop canvas where
   auditors lay out SOP steps as shapes and attach rules and runtime API tools.
3. **Runtime API Agent** — a small pipeline that executes registered HTTP
   endpoints against claims data at runtime, with auth resolution and caching.
4. **Execution Engine** — runs a claim through a built workflow, evaluating each
   bound SOP rule (LLM-assisted) and calling the rule's tools, producing an
   audit decision per claim.

---

## 2. Layout

```
uhc-backend-v2/
├── sop_backend/                 Django project (settings, URL root, Celery app)
├── builder/                     Workflow builder app (canvas CRUD, catalog, graph save)
├── sop_ingestion/               Ingestion REST surface + Celery glue + Audit* models
├── agent_tools/                 LangChain tool registry + DB-backed field mapping / ontology
├── execution_app/               Claim execution runs, rule evaluations, audit results
├── sop_ir/                      Pydantic IR schema + persistence
├── uhc-sop-ingestion/           Editable pkg: the LangGraph SOP ingestion pipeline + CLI
├── uhc-api-agent/               Editable pkg: runtime HTTP execution pipeline + CLI
├── uhc-execution-engine/        Editable pkg: claim → workflow execution + rule eval
├── yaml/                        Hand-authored SOP rule YAMLs + config
└── docs/                        Architecture, data model, setup, deployment guides
```

**Django apps** (`INSTALLED_APPS`): `sop_ingestion`, `builder`, `agent_tools`,
`execution_app`.

**Editable packages** (installed via `requirements.txt`):
`uhc-sop-ingestion`, `uhc-api-agent`, `uhc-execution-engine`.

---

## 3. Requirements

- Python **3.13**
- PostgreSQL 16, Redis 7, Neo4j 5, MongoDB 7, RabbitMQ 3.13
- Anthropic + OpenAI API keys (LLM enrichment and rule evaluation)

### Infrastructure via Docker

```bash
docker run -d -p 5432:5432  -e POSTGRES_PASSWORD=postgres postgres:16
docker run -d -p 6379:6379  redis:7
docker run -d -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/test1234 neo4j:5
docker run -d -p 27017:27017 mongo:7
docker run -d -p 5672:5672 -p 15672:15672 rabbitmq:3.13-management
```

---

## 4. Setup

```bash
# Virtualenv
python3.13 -m venv ../src
source ../src/bin/activate

# Dependencies (includes the three editable packages)
pip install -r requirements.txt

# Environment — create a .env (see §6 for the variables);
# .env.local and .env.prod are provided as references
cp .env.local .env          # then edit for your machine

# Database schema
PYTHONPATH=. python manage.py migrate

# Run the API
PYTHONPATH=. python manage.py runserver 0.0.0.0:8000
```

### Celery (required for SOP ingestion)

Ingestion uses a **master dispatcher**: Celery spawns one OS subprocess per
`job_id`, and the LangGraph pipeline runs in that subprocess (not in the worker).

```bash
PYTHONPATH=. celery -A sop_backend worker -Q job_queue,celery --concurrency=1 -l INFO

# Optional: cap parallel ingestion subprocesses (default 10)
export MAX_PIPELINE_SUBPROCESSES=10
```

### Frontend

```bash
cd ../../frontend/claims-frontend
yarn install
yarn dev        # http://localhost:5173
```

---

## 5. Architecture

### SOP Ingestion pipeline (`uhc-sop-ingestion`)

Entry point: `SopIngestionPipeline.run(seed_url)`. A LangGraph BFS loop over the
agent modules in `src/uhc_sop_ingestion/agents/` (21 modules across 18 stages,
`a01_intake` → `a18_ir_synthesis`):

```
intake → BFS: pick_next_url → fetch → [html|docx|xlsx|pdf parse] →
enrich (LLM) → context → validate → narrative → graph_synthesis →
[write_neo4j + write_postgres + write_mongo + write_redis] →
links → ir_synthesis → completion_check → loop or finalize
```

- **PDF native-vision door**: `a06c_pdf_perception` → `a06d_pdf_context_graph`
  → `a06e_pdf_synthesis` extract PDFs via Claude's document API.
- State uses `Annotated[list, operator.add]` accumulators so each agent appends.
- Every stage and LLM call is logged to Postgres (`PipelineStageLog`,
  `LLMCallLog`) and traced to **Langfuse**.

### Intermediate Representation (`sop_ir`)

Ingestion produces a Pydantic `SopIR` document (steps, decisions, routing /
`goto_step`, codes, references) — the structured, source-agnostic form the
builder and execution engine consume. **The execution engine reads bindings
from Postgres at runtime; it never re-reads the original PDF/DOCX/XLSX/HTML or
any YAML.**

### Workflow Builder (`builder`)

```
Workflow → WorkArea (lane) → Workbench (sub-canvas) → Shape (xyflow node)
Shape.properties JSONB: { sop_rules, tool_calls }
```

`ShapeDefinition.property_schema` (JSONB) drives the frontend inspector form, so
new shape fields need no frontend deploy.

Key endpoints:

```
GET  /api/builder/catalog/categories/      drag-palette groups
GET  /api/builder/catalog/shapes/          flat shape list
GET  /api/builder/ui/navigation/           sidebar nav
GET  /api/builder/workflows/<id>/graph/    full nested graph
PUT  /api/builder/workflows/<id>/graph/    atomic canvas save
GET  /api/builder/workflows/<id>/attachable/  per-node rule + tool picker
```

### Agent Tools (`agent_tools`)

`registry.py` maintains the tool factory list with lazy imports (one broken tool
never fails the whole registry); `sync_to_db()` upserts to the `Tool` table.
This app also owns the **DB-backed config** that the execution engine reads at
runtime — `SopFieldMapping` and `ClaimOntologyField` — editable from the UI.

```
POST /api/agent-tools/{name}/invoke
```

### Execution Engine (`uhc-execution-engine` + `execution_app`)

Runs a claim through a built workflow: loads each shape's rule bindings, gathers
tool results, and evaluates rules (Claude Sonnet, with injected domain
guidance) into an audit decision. Runs, rule evaluations, and decisions are
persisted in `execution_app`. All LLM calls are traced to **Langfuse**.

### Authentication

`builder/auth.py` implements `CorebackendJWTAuthentication` — it trusts HS256
JWTs minted by the Node `claims-corebackend` service. `JWT_SECRET` must match
between both services.

---

## 6. Environment variables

Create a `.env` (use `.env.local` / `.env.prod` as references). Key variables:

```
# Databases
PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DATABASE
REDIS_HOST / REDIS_PORT
NEO4J_HOST / NEO4J_PORT / NEO4J_USER / NEO4J_PASSWORD
MONGO_HOST / MONGO_PORT / MONGO_DATABASE
RABBITMQ_USER / RABBITMQ_PASSWORD / RABBITMQ_HOST / RABBITMQ_PORT

# LLM
ANTHROPIC_API_KEY
OPENAI_API_KEY

# Observability (Langfuse — self-hostable / open source)
LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST

# Django
DJANGO_SECRET_KEY / DJANGO_DEBUG / DJANGO_ALLOWED_HOSTS
CORS_ORIGINS=http://localhost:5173

# JWT (must match claims-corebackend)
JWT_SECRET

# Pipeline tuning
MAX_DEPTH=4
MAX_DOCS=200
MAX_PIPELINE_SUBPROCESSES=10
```

Tool-specific secrets go in `agent_tools/.env.tools`.

---

## 7. Common operations

### Trigger an ingestion

```bash
curl -X POST http://localhost:8000/api/ingest/ \
  -H 'Content-Type: application/json' \
  -d '{"seed_url": "http://localhost:9191/some_sop.html"}'
```

Or from the UI: create a workflow and attach one or more SOP URLs / uploads.

### Inspect the knowledge graph

```
GET /api/ingest/<job_id>/graph/                       JSON
GET /api/ingest/viewer/<job_id>/doc/<sop_id>/         HTML viewer
```

### CLIs (after the editable installs)

```bash
sop-ingest http://example.com/sop.html                # SOP ingestion
api-agent  call https://api.example.com/v1/endpoint --bearer sk-xyz
api-agent  register https://api.example.com/v1/endpoint --bearer sk-xyz
```

---

## 8. Tests

```bash
pytest agent_tools/tests/ -v
pytest execution_app/tests/ -v
pytest sop_ir/tests/ -v
```

---

## 9. Further docs

See `docs/` for deeper references: `ARCHITECTURE.md`, `DATA_MODEL.md`,
`SETUP.md`, `SOP_EXTRACTION.md`, `VM_DEPLOYMENT.md`, and `AGENT_TOOLS.md`.

---

*Built by toystack AI*
