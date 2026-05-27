# Setup

How to get this backend running on a fresh dev machine.

---

## 1. Prerequisites

* **Python 3.13** (the repo's `requirements.txt` and the editable packages pin it).
* **Conda** or any other venv tool (the team standard is `conda` — see §3).
* The shared `.env` file (already in the repo root). It points at hosted Postgres / Redis / Neo4j / Mongo / RabbitMQ on `toystack.store`, so you do **not** need to run those locally.
* For local-only mode, see §10.

---

## 2. Clone

```bash
git clone <repo-url> uhc-agentic-backend
cd uhc-agentic-backend
```

The repo expects the React SPA to live alongside it at `../../frontend/claims-frontend` if you want to run the full stack, but the backend works standalone.

---

## 3. Conda env

```bash
conda create -n uhc-agentic-backend python=3.13 -y
conda activate uhc-agentic-backend
```

(If you prefer `venv`, `python3.13 -m venv .venv && source .venv/bin/activate` works fine.)

---

## 4. Install dependencies

```bash
pip install -r requirements.txt
pip install -e uhc-sop-ingestion
pip install -e uhc-api-agent
pip install -e uhc-execution-engine
```

The editable installs let you edit agents under `uhc-sop-ingestion/src/`,
`uhc-api-agent/src/`, or `uhc-execution-engine/src/` and have Django pick
up changes immediately.

---

## 5. Environment file

The `.env` in the repo root already contains hosted-service credentials. Verify:

```bash
grep -E "^(PG_|REDIS_|NEO4J_|MONGO_|RABBITMQ_)" .env | wc -l
# Expect ~22 entries.
```

Critical keys:

| Variable           | Notes                                                                     |
|--------------------|---------------------------------------------------------------------------|
| `PG_*`             | Postgres — canonical store.                                              |
| `REDIS_*`          | Redis — cache + Celery result backend.                                   |
| `NEO4J_*`          | Neo4j — knowledge graph traversal.                                       |
| `MONGO_*`          | MongoDB — raw + parsed snapshots.                                        |
| `RABBITMQ_*`       | RabbitMQ — Celery broker.                                                |
| `OPENAI_API_KEY`   | For OpenAI GPT-4o calls in `a07_enrich` and `a08_context`.                |
| `ANTHROPIC_API_KEY`| For Claude calls (default LLM).                                           |
| `JWT_SECRET`       | **Must match the `claims-corebackend` HS256 secret** — at least 16 chars. |
| `DJANGO_SECRET_KEY`| Dev default works locally; set in production.                            |
| `DJANGO_DEBUG`     | `true` for dev. Disables the sync-run endpoint when false.                |
| `CORS_ORIGINS`     | Comma-separated allow-list for the SPA.                                  |
| `MAX_DEPTH`        | Default BFS link-hop limit (4).                                          |
| `MAX_DOCS`         | Hard cap on documents per job (200).                                     |
| `LLM_PROVIDER`     | `anthropic` (default) or `openai`.                                       |
| `LLM_MODEL`        | Default `claude-sonnet-4-5-20250929`.                                    |

The file is `.env`-gitignored — never commit it.

---

## 6. Migrate the database

```bash
PYTHONPATH=. python manage.py migrate
```

This applies migrations for all four apps (`builder`, `sop_ingestion`,
`agent_tools`, `execution_app`). Idempotent — re-run any time models change.

---

## 7. Seed the catalog

```bash
PYTHONPATH=. python manage.py seed_builder_catalog
```

Populates `ShapeCategory`, the 8 canonical `ShapeDefinition` palette items, `NavItem` sidebar entries, and `DashboardWidget` tiles. Idempotent. Also runs automatically post-migrate via the management command's `Meta.run_after_migrate` hook.

---

## 8. Run the Django server

```bash
PYTHONPATH=. python manage.py runserver 0.0.0.0:8000
```

Smoke-test:

```bash
curl http://localhost:8000/api/ingest/health/
# {"status":"ok","checks":{"uhc_sop_ingestion":"ok","celery":"no_workers"},...}
```

`celery: "no_workers"` is expected if you haven't started a worker yet — see next step.

---

## 9. Run the Celery worker

In a separate terminal (same `.env`, same conda env):

```bash
PYTHONPATH=. celery -A sop_backend worker -l INFO
```

Health check should now show `celery: "ok"`.

You can run the worker with multiple processes (`--concurrency=4`) but each task takes 60–180 s of CPU, so parallelism beyond your CPU core count won't help.

---

## 10. (Optional) Run services locally with Docker

If `toystack.store` is unreachable or you want to develop offline, override `.env` to point at local containers:

```bash
docker run -d --name pg     -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16
docker run -d --name redis  -p 6379:6379 redis:7
docker run -d --name neo4j  -p 7474:7474 -p 7687:7687 \
       -e NEO4J_AUTH=neo4j/test1234 neo4j:5
docker run -d --name mongo  -p 27017:27017 mongo:7
docker run -d --name rmq    -p 5672:5672  rabbitmq:3
```

Then in `.env`:

```
PG_HOST=localhost
PG_PORT=5432
PG_USER=postgres
PG_PASSWORD=postgres
PG_DATABASE=postgres
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_USER=
REDIS_PASSWORD=
NEO4J_HOST=localhost
NEO4J_PORT=7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=test1234
MONGO_HOST=localhost
MONGO_PORT=27017
MONGO_USER=
MONGO_PASSWORD=
MONGO_DATABASE=sop_ingestion
RABBITMQ_HOST=localhost
RABBITMQ_PORT=5672
RABBITMQ_USER=guest
RABBITMQ_PASSWORD=guest
```

Then re-run `migrate`, `seed_builder_catalog`, `runserver`, and `celery worker`.

---

## 11. (Optional) Run the frontend

```bash
cd ../../frontend/claims-frontend
yarn install
yarn dev   # http://localhost:5173
```

The SPA expects:

* `VITE_BACKEND_URL=http://localhost:8000` (or whatever you bound Django to).
* `VITE_AUTH_URL=http://localhost:4000` (the Node `claims-corebackend`).

JWTs minted by the Node service are accepted by Django via the `JWT_SECRET` shared in `.env`.

---

## 12. End-to-end smoke test

```bash
# 1. Start an ingestion job (uses a sample SOP — adjust URL to one you have)
curl -X POST http://localhost:8000/api/ingest/ \
  -H 'Content-Type: application/json' \
  -d '{"seed_url": "http://localhost:9191/obh_facets_timely_filing.html"}'
# → 202 with job_id

# 2. Poll until COMPLETED (~60–180 s)
curl http://localhost:8000/api/ingest/<job_id>/ | jq .status

# 3. Inspect parsed sections
curl http://localhost:8000/api/ingest/<job_id>/sections/ | jq '.title, (.steps | length)'

# 4. Inspect the knowledge graph
curl http://localhost:8000/api/ingest/<job_id>/graph/ | jq '.nodes | length, .edges | length'

# 5. Open the HTML viewer
open http://localhost:8000/api/ingest/viewer/

# 6. (Once a workflow has rules attached to its Shapes) Run a batch through
#    the execution engine. Prepare a small claims.xlsx with header `subscriber_id`
#    and 2–3 ids drawn from agent_tools/mock/ fixtures.
curl -F file=@claims.xlsx -F claim_id_column=subscriber_id \
     -X POST http://localhost:8000/api/execute/workflows/<workflow_id>/run-batch/ \
  | jq '.status, (.results | length)'
```

---

## 13. Common issues

* **`MISSING — No module named 'uhc_sop_ingestion'`** in health check.
  You forgot `pip install -e uhc-sop-ingestion`. Re-run, restart the Django server.

* **`celery: "no_workers"`** in health check.
  Start the Celery worker (`celery -A sop_backend worker -l INFO`).

* **`JWT_SECRET env var missing or too short`** when hitting builder endpoints.
  Set `JWT_SECRET` in `.env` (≥ 16 chars) and match it on the Node corebackend. Without this, every authenticated endpoint will 500.

* **Migrations stuck on `builder` app.**
  Drop the `builder_*` tables (in dev only) and re-run `migrate`.

* **`psycopg2.errors.OperationalError: SSL connection has been closed unexpectedly`** during long ingestion runs.
  The hosted Postgres in `toystack.store` may rate-limit. Retry or fall back to local Docker (§10).

* **Pipeline crashes on the first LLM call.**
  Check `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are populated. The pipeline tolerates one provider being unreachable (cross-provider fallback), but not both.

* **HTML viewer renders blank.**
  CSRF or `X-Frame-Options` collisions are the usual cause. The viewer is `@xframe_options_exempt`; if you embed it elsewhere, ensure the embedding origin matches `CORS_ORIGINS`.
