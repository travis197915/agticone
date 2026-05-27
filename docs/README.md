# UHC Agentic Backend — Docs

Documentation for the **Wipro UHC Agentic Claims-Audit Platform** backend.

The repo at a glance: a Django REST service plus three LangGraph packages.

* Ingest any HTML / DOCX / XLSX / PDF SOP into a structured Postgres + Neo4j knowledge graph.
* Drive a server-driven workflow builder where each canvas node can attach individual SOP rules and live HTTP "tool calls".
* Execute a workflow against an Excel of claim ids: per-claim, per-Shape rule evaluation with LLM-driven verdicts and a full audit trail.
* Track every run with real-time stage logs, LLM-call telemetry, and an HTML viewer.

---

## Table of contents

| Doc                                  | What it covers                                                                |
|--------------------------------------|-------------------------------------------------------------------------------|
| [ARCHITECTURE.md](ARCHITECTURE.md)   | Components, datastores, auth, request flows, code layout, design rationale.  |
| [SETUP.md](SETUP.md)                 | Step-by-step dev setup (conda env, migrate, seed, runserver, Celery worker). |
| [API.md](API.md)                     | Every REST endpoint with request / response examples and error codes.        |
| [openapi.yaml](openapi.yaml)         | Machine-readable spec — drop into Postman / Swagger UI.                      |
| [DATA_MODEL.md](DATA_MODEL.md)       | Every Django model, every field, JSONB shapes, foreign-key map.              |
| [PIPELINE.md](PIPELINE.md)           | The 122-agent LangGraph ingestion pipeline, stage by stage.                  |
| [EXECUTION_ENGINE.md](EXECUTION_ENGINE.md) | The per-Shape execution engine — batch claim adjudication.            |
| [AGENT_TOOLS.md](AGENT_TOOLS.md)     | The DB-backed LangChain tool registry + per-node binding tables.            |
| [AGENTS.md](AGENTS.md)               | Per-agent reference — all 122 agents with inputs / outputs / side effects.  |
| [RUNTIME_AGENT.md](RUNTIME_AGENT.md) | The `uhc-api-agent` package — runtime HTTP "tool calls".                     |
| [STORAGE.md](STORAGE.md)             | Existing doc — datastore conventions.                                        |

---

## Quick start

```bash
conda create -n uhc-agentic-backend python=3.13 -y
conda activate uhc-agentic-backend

pip install -r requirements.txt
pip install -e uhc-sop-ingestion
pip install -e uhc-api-agent
pip install -e uhc-execution-engine

PYTHONPATH=. python manage.py migrate
PYTHONPATH=. python manage.py seed_builder_catalog
PYTHONPATH=. python manage.py runserver 0.0.0.0:8000          # terminal 1
PYTHONPATH=. celery -A sop_backend worker -l INFO              # terminal 2

curl http://localhost:8000/api/ingest/health/
```

Full setup including hosted-vs-local services in [SETUP.md](SETUP.md).

---

## Triggering an ingestion

```bash
curl -X POST http://localhost:8000/api/ingest/ \
  -H 'Content-Type: application/json' \
  -d '{
        "seed_url": "https://example.com/sop.html",
        "max_depth": 4,
        "max_docs": 200
      }'
# → 202 Accepted with a job_id

curl http://localhost:8000/api/ingest/<job_id>/ | jq .status
# Poll until "COMPLETED" (~60–180 s)

curl http://localhost:8000/api/ingest/<job_id>/graph/    # knowledge graph
curl http://localhost:8000/api/ingest/<job_id>/sections/ # structured sections
open http://localhost:8000/api/ingest/viewer/            # HTML viewer
```

Full API surface (with auth, sample bodies, error cases) in [API.md](API.md).

---

## Executing a workflow

Once a workflow has SOP rules attached to its Shapes (via the SPA), run a
batch of claims through it:

```bash
curl -F file=@claims.xlsx -F claim_id_column=subscriber_id \
     -X POST http://localhost:8000/api/execute/workflows/<workflow_id>/run-batch/
```

For each row in the Excel, the engine fetches the claim via `linx_claim_search`,
walks the workflow's Shapes in canvas order, runs one LLM call per attached
rule with that Shape's tools in context, and halts the claim on any DENY/STOP
match (`status: TERMINATED_EARLY`). Full audit trail in
`RuleExecutionRun` + `RuleEvaluation` + `ToolInvocationRecord` + `LLMCallLog`.

Full execution engine reference in [EXECUTION_ENGINE.md](EXECUTION_ENGINE.md).

---

## Architecture at a glance

```
   React SPA ──► /api/builder + /api/ingest ──► Django ──► Celery ──► 122-agent LangGraph
                       + /api/execute              │                        │
                                                   ▼                        ▼
                          Postgres (canon) · Neo4j (graph) · Mongo (raw + parsed) · Redis (queue + cache)
                                                   │
                                                   ▼
                          /api/execute/workflows/<id>/run-batch/  ──►  uhc-execution-engine
                                                                       (6-node LangGraph, per-Shape eval)
```

* **Postgres** = single source of truth. Workflows, SOP entities, materialised graph, stage + LLM logs.
* **Neo4j** = Cypher-queryable mirror of the materialised graph.
* **MongoDB** = append-only raw + parsed snapshots per document, for debug / replay.
* **Redis** = BFS queue persistence, agentic-graph shared context, live job-progress hash.
* **RabbitMQ** = Celery broker.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full picture (auth, request flows, design decisions).

---

## Pipeline at a glance

The ingestion pipeline is a 17-stage LangGraph state machine. 122 agent functions across 17 modules (`a01_intake.py` → `a17_narrative.py`).

```
intake → pick_next_url → fetch → parse(html|docx|xlsx|pdf)
       → enrich → context → validate → narrative → graph_synthesis
       → write_neo4j → write_postgres → write_mongo → write_redis
       → link_stage → completion_check → (loop back or final_stage)
```

Per-stage detail in [PIPELINE.md](PIPELINE.md). Per-agent reference in [AGENTS.md](AGENTS.md).

---

## Importing into Postman

1. Open Postman.
2. *Import* → *Files* → select `docs/openapi.yaml`.
3. Set the `bearerAuth` variable on the resulting collection to a valid JWT from the Node `claims-corebackend`.
4. Start hitting endpoints. Sample request bodies are embedded in the spec.

The same file works in Swagger UI, Insomnia, Redoc, and most other API tooling.

---

## Reading guide

* **New contributor?** [ARCHITECTURE.md](ARCHITECTURE.md) → [SETUP.md](SETUP.md) → [API.md](API.md).
* **Debugging an ingestion?** [PIPELINE.md](PIPELINE.md) → [AGENTS.md](AGENTS.md) (find the failing stage / agent) → the HTML viewer at `/api/ingest/viewer/<job_id>/`.
* **Debugging an execution run?** [EXECUTION_ENGINE.md](EXECUTION_ENGINE.md) → query `RuleExecutionRun` / `RuleEvaluation` / `LLMCallLog` (`WHERE execution_run_id=...`) for the offending run.
* **Adding a new endpoint?** [API.md](API.md) → update [openapi.yaml](openapi.yaml).
* **Adding a new pipeline agent?** [AGENTS.md](AGENTS.md) for the convention; wire into [graph.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/graph.py).
* **Adding a new workflow shape?** It's data, not code — `INSERT INTO builder_shape_definition` (or extend [catalog_seed.py](../builder/catalog_seed.py) for an idempotent seed).

---

## Conventions

* **Code references** use `path:line` so they're clickable in editors:
  e.g. [sop_ingestion/views.py:47](../sop_ingestion/views.py#L47).
* **`AuditGraphNode` + `AuditGraphEdge`** are the single source of truth for the SPA's React-Flow viewer. Neo4j is a mirror.
* **Edges live in `localStorage` during editing**, persist on canvas save (`PUT /workflows/<id>/graph/`).
* **Every LLM call** routes through `_llm_call()` in [a07_enrich.py](../uhc-sop-ingestion/src/uhc_sop_ingestion/agents/a07_enrich.py) (ingestion) or `llm_call()` in [llm.py](../uhc-execution-engine/src/uhc_execution_engine/llm.py) (execution engine): retry × 2 → cross-provider fallback → schema validate → `LLMCallLog` row (FK to `IngestionJob` for ingestion calls, FK to `RuleExecutionRun` for engine calls).
* **Every stage** writes a `PipelineStageLog` row with `started_at` / `completed_at` / `duration_ms` — visible while the job is still running.

---

## Project structure (top level)

```
uhc-agentic-backend/
├── manage.py
├── requirements.txt
├── .env                        ── hosted-service credentials (gitignored)
├── sop_backend/                ── Django project
├── builder/                    ── /api/builder/ app
├── sop_ingestion/              ── /api/ingest/ app
├── agent_tools/                ── /api/agent-tools/ app — tool registry + node bindings
├── execution_app/              ── /api/execute/ app — batch / run / evaluation tables
├── uhc-sop-ingestion/          ── editable package — 122-agent pipeline
├── uhc-api-agent/              ── editable package — runtime HTTP agent
├── uhc-execution-engine/       ── editable package — 6-node per-Shape execution graph
└── docs/                       ── this folder
```

Full file map in [ARCHITECTURE.md §6](ARCHITECTURE.md#6-code-organization).
