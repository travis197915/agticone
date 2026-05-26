# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**Wipro UHC Agentic Claims-Audit Platform** — a Django backend that powers a claims-audit workflow system. Three main capabilities:

1. **Workflow Builder** — REST API for a drag-drop canvas where auditors attach SOP rules and runtime API agents to workflow shapes
2. **SOP Ingestion Pipeline** — 122-agent LangGraph pipeline that crawls HTML/DOCX/XLSX/PDF documents, parses structure, enriches with LLMs (Anthropic + OpenAI), and writes to Neo4j + Postgres + MongoDB + Redis
3. **Runtime API Agent** — 5-agent pipeline (`uhc-api-agent` package) that executes registered HTTP endpoints against claims data at runtime

---

## Commands

```bash
# Setup (first time)
python3.13 -m venv ../src
source ../src/bin/activate
pip install -r requirements.txt
pip install -e uhc-sop-ingestion
pip install -e uhc-api-agent

# Database
PYTHONPATH=. python manage.py migrate
PYTHONPATH=. python manage.py seed_builder_catalog  # populate shape palette + nav

# Run Django
PYTHONPATH=. python manage.py runserver 0.0.0.0:8000

# Run Celery worker (separate terminal — required for SOP ingestion)
PYTHONPATH=. celery -A sop_backend worker -l INFO

# Tests
pytest builder/tests_smoke.py -v
pytest agent_tools/tests/ -v

# SOP ingestion CLI (after pip install -e uhc-sop-ingestion)
sop-ingest http://example.com/sop.html

# API agent CLI (after pip install -e uhc-api-agent)
api-agent call https://api.example.com/v1/endpoint --bearer sk-xyz
api-agent register https://api.example.com/v1/endpoint --bearer sk-xyz
```

**Required infrastructure (Docker):**
```bash
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16
docker run -d -p 6379:6379 redis:7
docker run -d -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/test1234 neo4j:5
docker run -d -p 27017:27017 mongo:7
docker run -d -p 5672:5672 -p 15672:15672 rabbitmq:3.13-management
```

---

## Architecture

### Django Apps

| App | Purpose |
|-----|---------|
| `builder/` | Workflow canvas CRUD; server-driven shape palette; graph save |
| `sop_ingestion/` | Ingestion job management; Celery tasks that invoke the LangGraph pipeline |
| `agent_tools/` | LangChain tool registry; runtime tool invocation endpoint |

### Installable Packages (editable installs in requirements.txt)

| Package | Path | Purpose |
|---------|------|---------|
| `uhc-sop-ingestion` | `uhc-sop-ingestion/` | 122-agent LangGraph pipeline; standalone CLI |
| `uhc-api-agent` | `uhc-api-agent/` | 5-agent HTTP execution pipeline; standalone CLI |

### Workflow Builder Data Model

```
Workflow
  └─ WorkArea (swim lane / phase)
      └─ Workbench (sub-canvas)
          └─ Shape (xyflow node; FK → ShapeDefinition)
               └─ properties JSONB: { sop_rules, tool_calls }
```

`ShapeDefinition` has a `property_schema` JSONB field that drives the frontend inspector form — adding a new field to a shape requires no frontend deployment.

**Key REST endpoints:**
```
GET  /api/builder/catalog/categories/     — drag palette groups
GET  /api/builder/catalog/shapes/         — flat shape list
GET  /api/builder/ui/navigation/          — sidebar nav items
GET  /api/builder/workflows/<id>/graph/   — full nested graph
PUT  /api/builder/workflows/<id>/graph/   — atomic canvas save
GET  /api/builder/workflows/<id>/attachable/ — per-node rule + tool picker
```

### SOP Ingestion Pipeline (uhc-sop-ingestion)

Entry point: `SopIngestionPipeline.run(seed_url)` in `uhc-sop-ingestion/src/uhc_sop_ingestion/pipeline.py`

The pipeline is a LangGraph BFS loop across 17 stages (122 agent functions in `agents/a01_intake.py` through `agents/a17_narrative.py`):

```
intake → BFS loop: pick_next_url → fetch → [html|docx|xlsx|pdf parse] →
enrich (LLM) → context → validate → narrative → graph_synthesis →
[write_neo4j + write_postgres + write_mongo + write_redis] → link → completion_check → loop or final
```

State uses `Annotated[list, operator.add]` accumulators so each agent appends rather than overwrites. All agent executions are logged in real time to `PipelineStageLog` and `LLMCallLog` Postgres tables.

**LLM strategy:**
- **Anthropic (Claude Sonnet)** — reasoning, classification, narrative writing
- **OpenAI (GPT-4o)** — structured JSON extraction
- Fallback chain: primary provider ×2 retries → cross-provider fallback

### Runtime API Agent (uhc-api-agent)

5-agent pipeline: `validate_url → resolve_auth → api_caller → json_parser → response_logger`

Auth modes: `bearer`, `basic`, `api_key`, `custom`. Results cached in Redis (24h), persisted to Postgres + MongoDB.

### Agent Tools Registry

`agent_tools/registry.py` maintains `_TOOL_FACTORIES` — a list of `(module_path, builder_fn, returns_list)` tuples. Lazy imports prevent one broken tool from failing the whole registry. `sync_to_db()` does an idempotent upsert to the `agent_tools.Tool` table.

Tool endpoint: `POST /api/agent-tools/{name}/invoke`

### Authentication

`builder/auth.py` implements `CorebackendJWTAuthentication` — trusts HS256 JWTs minted by the Node `claims-corebackend` service. The `JWT_SECRET` must match between both services.

---

## Environment Variables

Copy from `.env.example`. Key variables:

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

# Django
DJANGO_SECRET_KEY / DJANGO_DEBUG / DJANGO_ALLOWED_HOSTS
CORS_ORIGINS=http://localhost:5173

# JWT (must match claims-corebackend)
JWT_SECRET

# Pipeline tuning
MAX_DEPTH=4
MAX_DOCS=200
```

Tool-specific secrets go in `agent_tools/.env.tools`.
