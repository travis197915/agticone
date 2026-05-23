# Agent Tools — API, Catalog & Integration Guide

The `agent_tools` Django app exposes the **18 LangChain tools** ported from
`extracted-tools-main/` as a first-class, DB-backed registry behind a small
REST surface, runs each invocation through a one-node **LangGraph** runtime,
and ships a **mock upstream server** so the entire catalog runs end-to-end
without any outbound network.

This guide is the single source of truth for:

- The REST API (`/api/agent-tools/...`).
- The 18-tool catalog and how to invoke each one.
- The mock upstream server (`/api/mocks/...`) and the `.env.tools` switch
  that flips it to real upstreams.
- The relational model (`Tool`, `NodeRuleBinding`, `NodeToolBinding`) that
  replaces the old `Shape.properties.{sop_rules,tool_calls}` JSON blobs.
- The SPA wiring (NodePalette → RulePicker → ConfigPanel → ToolInvokeModal).
- Local development: migrations, seed sync, smoke test.

For the **per-tool field-level schemas** (every request key, every response
key, error envelopes) keep referring to
`extracted-tools-main/docs/TOOLS.md`. This document covers the runtime that
hosts those tools.

---

## 1. Architecture at a glance

```
┌──────────────────────────────────────────────────────────────────────────┐
│ React SPA (claims-frontend)                                              │
│   • NodePalette       — left sidebar tool registry                       │
│   • RulePicker        — "attach SOP rule" modal, with tool picker        │
│   • ConfigPanel       — per-node rule/tool list (grouped)                │
│   • ToolInvokeModal   — schema-driven test form                          │
└────────────┬─────────────────────────────────────────────────────────────┘
             │ JWT  (HS256, JWT_SECRET shared with Node core-backend)
             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ Django (sop_backend) :8000                                               │
│                                                                          │
│  /api/agent-tools/                ← registry + invoke                    │
│      GET   ""                     list active Tool rows                  │
│      GET   "{name}/"              full tool descriptor (args_schema)     │
│      POST  "{name}/invoke"        run through LangGraph                  │
│                                                                          │
│  /api/agent-tools/.../invoke ──► single_tool_graph (StateGraph)          │
│      └─► langchain StructuredTool ──► requests ──► upstream URL          │
│                                                  │                       │
│                                                  ▼                       │
│  /api/mocks/                  ◄── same process. .env.tools points here   │
│      doc360 / facets / cbd / diagnosis / linx / cms / fep / npi          │
│                                                                          │
│  agent_tools schema (Postgres)                                           │
│    tool                ┐                                                 │
│    node_rule_binding   │  isolated under `agent_tools` schema; resolved  │
│    node_tool_binding   ┘  via `-c search_path=public,agent_tools`        │
└──────────────────────────────────────────────────────────────────────────┘
```

Three things to keep in mind:

1. **One process, two surfaces.** In dev, `agent-tools/...` and `mocks/...`
   are served by the same Django dev server. The tools resolve their
   upstream URLs from `.env.tools`, which defaults every host to
   `http://localhost:8000/api/mocks/...` — so a tool call loops back into
   the same process.
2. **The Tool table is the contract.** The SPA never reads the LangChain
   catalog directly. It calls `GET /api/agent-tools/`, which returns rows
   from `agent_tools.tool`. New tools land in the table via
   `0002_seed_registry` or `manage.py sync_tool_registry`.
3. **Attaching a tool to a node is a row, not a JSON blob.** The legacy
   `Shape.properties.tool_calls` array is replaced by `NodeToolBinding`,
   with an optional FK to `NodeRuleBinding` (the "tools I picked while
   attaching that rule" relationship).

---

## 2. Folder layout

```
agent_tools/
├── apps.py                   AppConfig — loads .env.tools on ready()
├── env.py                    Tiny dotenv loader (no Django imports)
├── .env.tools                Every upstream URL the 18 tools call
├── models.py                 Tool, NodeRuleBinding, NodeToolBinding
├── serializers.py            DRF serializers for the three models
├── views.py                  ToolListView / ToolDetailView / ToolInvokeView
├── urls.py                   Mounted at /api/agent-tools/
├── registry.py               iter_tools() + sync_to_db()
├── admin.py
├── management/commands/sync_tool_registry.py
├── migrations/
│   ├── 0000_create_schema.py          CREATE SCHEMA agent_tools
│   ├── 0001_initial.py                tool, node_rule_binding, node_tool_binding
│   ├── 0002_seed_registry.py          calls registry.sync_to_db()
│   ├── 0003_migrate_shape_properties.py  move legacy JSON blobs into rows
│   └── 0004_move_to_agent_tools_schema.py  brownfield: ALTER ... SET SCHEMA
├── graphs/
│   └── single_tool_graph.py  one-node LangGraph wrapper around a StructuredTool
├── tools/                    the 18 LangChain tools
│   ├── _http.py              requests with retry / structured logging
│   ├── _cache.py             in-process TTL cache (Facets/CBD/LINX tokens)
│   ├── _sql_memory.py        pymssql-free in-memory backend
│   ├── _logging.py
│   ├── doc360_tool.py
│   ├── facets_tool.py                 (6 tools)
│   ├── facet_extension_portal_tool.py (3 tools)
│   ├── cbd_tool.py                    (check_medicare_coverage)
│   ├── diagnosis_tool.py              (check_diagnosis_coverage)
│   ├── linx_tool.py
│   ├── opt_out_tool.py
│   ├── cross_prevalence_billing_tool.py
│   ├── sop_step_persistence_tool.py
│   ├── llm_claim_parser.py
│   ├── electronic_claim_parser.py
│   └── schemas/              Pydantic input/output models per tool
├── mock/                     mock upstream server
│   ├── urls.py               mounted at /api/mocks/
│   ├── _loader.py            loads fixtures/*.json
│   ├── fixtures/             canned JSON responses keyed by URL parts
│   ├── views_doc360.py
│   ├── views_facets.py
│   ├── views_fep.py
│   ├── views_cbd.py
│   ├── views_diagnosis.py
│   ├── views_linx.py
│   ├── views_cms.py
│   ├── views_npi.py
│   └── smoke.py              standalone Python smoke driver (18 calls)
└── tests/
    ├── test_models.py
    ├── test_registry_endpoints.py
    ├── test_tools_end_to_end.py
    └── test_graph_serialization.py
```

---

## 3. REST API — `/api/agent-tools/`

All three endpoints require `Authorization: Bearer <jwt>` (HS256, signed
with `JWT_SECRET`, same secret the Node core-backend uses).

### 3.1 `GET /api/agent-tools/`

List active tools. Used by the SPA to populate the NodePalette and the
RulePicker tool list.

**Response** (array):

```json
[
  {
    "id": "f7e8...uuid",
    "name": "facets_get_summary",
    "display_name": "Facets Get Summary",
    "description": "Return the Facets claim-summary envelope for ...",
    "kind": "langchain",
    "tool_kind": "langchain",
    "invoke_url": "/api/agent-tools/facets_get_summary/invoke",
    "args_schema": { "type": "object", "properties": { "claim_number": { "type": "string" } }, "required": ["claim_number"] },
    "metadata": {},
    "endpoint_id": "",
    "is_active": true,
    "created_at": "2026-05-23T15:10:55Z",
    "updated_at": "2026-05-23T15:10:55Z"
  }
]
```

Notes:

- `kind == "langchain"` for the 18 ported tools; `kind == "api_agent"`
  for runtime HTTP agents registered by the legacy `ApiAgentPipeline`.
- `args_schema` is the Pydantic-derived JSON Schema and is what
  `ToolInvokeModal` renders into a form.
- `invoke_url` is always relative; the SPA prefixes it with its API base.

### 3.2 `GET /api/agent-tools/{name}/`

Full descriptor for a single tool. Used by `ToolInvokeModal` when the user
clicks **Test** on a NodePalette row.

Returns the same shape as one element of the list. `404` if the tool name
is unknown.

### 3.3 `POST /api/agent-tools/{name}/invoke`

Run the tool through the single-tool LangGraph and return its raw output.

**Request:**

```json
{ "args": { "claim_number": "25XG44660400" } }
```

`args` must be a JSON object; it is passed as keyword arguments to the
underlying `StructuredTool`. Anything outside the tool's `args_schema` is
ignored by Pydantic.

**Response, success (HTTP 200):**

```json
{
  "ok": true,
  "tool": "facets_get_summary",
  "result": {
    "status_code": 200,
    "status_message": null,
    "body": { "Data": { "ClaimSummary": { "...": "..." } } },
    "claim_number": "25XG44660400",
    "endpoint": "summary",
    "timestamp": 1779549210
  }
}
```

`result` is whatever the LangChain tool returns — usually a dict that
follows a Pydantic schema in `agent_tools/tools/schemas/`. For
`medicare_optout_checker` it is a JSON-encoded **string** (legacy contract);
the SPA `JSON.parse`s it before rendering.

**Response, failure:**

| Condition                            | HTTP | Body                                                                |
|--------------------------------------|------|---------------------------------------------------------------------|
| Unknown / inactive tool name         | 404  | `{ "ok": false, "error": "unknown tool '...'" }`                    |
| `args` not a JSON object             | 400  | `{ "ok": false, "error": "'args' must be a JSON object" }`          |
| Tool / upstream raised an exception  | 500  | `{ "ok": false, "tool": "...", "error": "<exception message>" }`    |

A logging exception is also written server-side
(`logger.exception("tool '%s' invoke failed", ...)`) with the full
traceback, so the SPA never has to display a stack trace.

---

## 4. The 18-tool catalog (quick index)

Quick reference for what each registered tool does. **Field-level request
and response schemas live in
`extracted-tools-main/docs/TOOLS.md`** — that document is generated from
the same Pydantic models the tool registry serializes.

| # | Tool name                                     | Module                              | What it does (one line)                                              |
|---|-----------------------------------------------|-------------------------------------|----------------------------------------------------------------------|
| 1 | `doc360_read_claim_by_fln_dcc`                | `doc360_tool.py`                    | Read DOC360 claim print-image by FLN/DCC.                            |
| 2 | `facets_get_summary`                          | `facets_tool.py`                    | Facets claim-summary header.                                         |
| 3 | `facets_get_cob`                              | `facets_tool.py`                    | Coordination-of-Benefits data.                                       |
| 4 | `facets_get_line_details`                     | `facets_tool.py`                    | All service-line details (iterates seq 1..N).                        |
| 5 | `facets_get_member_eligibility`               | `facets_tool.py`                    | Member eligibility (resolves MEME_CK from summary).                  |
| 6 | `facets_get_provider_details`                 | `facets_tool.py`                    | Provider search via stored proc + DOC360 NPI filter.                 |
| 7 | `facets_get_duplicate_claim`                  | `facets_tool.py`                    | Duplicate-claim search filtered by line DOS.                         |
| 8 | `facet_extension_portal_provider`             | `facet_extension_portal_tool.py`    | BH provider complete list by PRPR ID.                                |
| 9 | `facet_extension_portal_programme`            | `facet_extension_portal_tool.py`    | Programme details by Program Detailed ID.                            |
| 10| `facet_ext_portal_group_model`                | `facet_extension_portal_tool.py`    | Network fee-schedule group model.                                    |
| 11| `check_medicare_coverage`                     | `cbd_tool.py`                       | CBD CPT coverage check (LOB-aware).                                  |
| 12| `check_diagnosis_coverage`                    | `diagnosis_tool.py`                 | ICD diagnosis coverage check.                                        |
| 13| `linx_claim_search`                           | `linx_tool.py`                      | LINX BH claim search by subscriber.                                  |
| 14| `medicare_optout_checker`                     | `opt_out_tool.py`                   | CMS Medicare provider opt-out lookup.                                |
| 15| `check_cross_prevalence_billing`              | `cross_prevalence_billing_tool.py`  | Cross-Billing Prevailing Code lookup.                                |
| 16| `save_sop_step`                               | `sop_step_persistence_tool.py`      | Persist one SOP-step execution row.                                  |
| 17| `llm_parse_claim_with_ontology`               | `llm_claim_parser.py`               | LLM-first HCFA extraction (ontology + JSON schema).                  |
| 18| `claim_parse_flat_template_with_confidence`   | `electronic_claim_parser.py`        | Regex-first HCFA parse (optional LLM merge).                         |

### One canonical request per tool

The exact bodies used by the smoke driver — handy as copy-paste starters
for `curl`, `ToolInvokeModal`, or unit tests.

```json
// 1
{ "args": { "fln_dcc": "1234567890" } }
// 2-7  (all six Facets tools take the same key)
{ "args": { "claim_number": "25XG44660400" } }
// 6 takes "claim_number_for_reference" instead:
{ "args": { "claim_number_for_reference": "25XG44660400" } }
// 8
{ "args": { "provider_id": "FAC000022500" } }
// 9
{ "args": { "program_detailed_id": "276728" } }
// 10
{ "args": { "claim_number": "25XG44660400" } }
// 11
{ "args": { "cpt_codes": ["99213", "99214"], "group_name": "Standard Medicare", "plan_name": "Standard Medicare" } }
// 12
{ "args": { "diagnosis_code": "E11.9" } }
// 13
{ "args": {
    "subscriber_id": "SUB-12345", "first_name": "JANE", "last_name": "DOE",
    "dob": "01/24/1980", "start_date": "01/01/2025", "end_date": "01/31/2025"
} }
// 14
{ "args": { "last_name": "DOE", "state": "MA" } }
// 15
{ "args": { "cpt_code_a": "99213", "cpt_code_b": "99214" } }
// 16  (see agent_tools/mock/smoke.py for the full SopStepInput shape)
// 17
{ "args": { "claim_data": { "content": "MOCK PRINT IMAGE\nTOTAL CHARGE $150.00\n21 DIAGNOSIS 1 F33.2 2 E11.8\n" } } }
// 18
{ "args": { "claim_data": { "content": "Box 21: 1 I10 2 E11.9\nTOTAL CHARGE $250.00\n" } } }
```

---

## 5. Mock upstream server — `/api/mocks/`

Every upstream the 18 tools talk to has a Django view + a JSON fixture in
`agent_tools/mock/`. Mounted from `sop_backend/urls.py` under `/api/mocks/`.

| Upstream  | Routes (under `/api/mocks/`)                                                                 | View module           |
|-----------|----------------------------------------------------------------------------------------------|-----------------------|
| DOC360    | `doc360/security/tokens`, `doc360/api/ecs/doc360-getcontent/v1/document-contents/read`        | `views_doc360.py`     |
| Facets    | `facets/security/tokens`, `facets/Claims/{claim}/Inquiry/Summary`, `.../COB`, `.../Lines/{seq}/Details`, `.../Members/Coverage/MemberKey/{mk}/Eligibility`, `facets/data/procedure/execute`, `facets/Search/Claims/Inquiry` | `views_facets.py` |
| FEP       | `fep/getCompleteList/{provider_id}`, `fep/getPrgm/{program_detailed_id}`, `fep/checkModel/{prpr_id}` | `views_fep.py`        |
| CBD       | `cbd/oauth/token`, `cbd/coverage`, `cbd/customer-info`                                        | `views_cbd.py`        |
| Diagnosis | `diagnosis`                                                                                  | `views_diagnosis.py`  |
| LINX      | `linx/oauth/token`, `linx/claim-search`                                                       | `views_linx.py`       |
| CMS       | `cms/{dataset_id}/data`                                                                       | `views_cms.py`        |
| NPI       | `npi/api/`                                                                                    | `views_npi.py`        |

### Adding a new fixture

1. Drop a JSON file under `agent_tools/mock/fixtures/<upstream>/`.
2. Reference it from the matching view with `_loader.load_fixture(...)`.
3. The mock view stays small — it picks fixture, optionally filters by
   query/JSON params, returns `JsonResponse`.

### Why the loopback works

The mock endpoints live in the **same Django process** as the tool
invoker. When the LangGraph node calls `requests.post(...)`, the request
hits a wsgi handler thread on the same `:8000` port that is currently
busy executing the tool. The dev server is multithreaded by default, so
this self-call works. **Do not run `manage.py runserver --nothreading`**
or the smoke test will deadlock.

---

## 6. Environment variables — `agent_tools/.env.tools`

Loaded by `AgentToolsConfig.ready()` with `override=False`, so anything
already in `os.environ` (or your project `.env`) wins.

```dotenv
# DOC360
DOC360_API_BASE=http://localhost:8000/api/mocks/doc360
DOC360_TOKEN_URL=http://localhost:8000/api/mocks/doc360/security/tokens
DOC360_READ_DOCUMENT_CONTENT=/api/ecs/doc360-getcontent/v1/document-contents/read
DOC360_CLIENT_ID=mock
DOC360_CLIENT_SECRET=mock
DOC360_SCOPE=mock
DOC360_APP_ID=mock
DOC360_USER_ID=mock
UPSTREAM_ENV=mock

# Facets
FACETS_BASE_URL=http://localhost:8000/api/mocks/facets
FACETS_USERNAME=mock
FACETS_PASSWORD=mock
FACETS_REGION=us
FACETS_IDENTITY=mock
FACETS_SIGNON_METHOD=mock
SSL_VERIFY=false
MAX_LINE_SEQ=100

# Facet Extension Portal
FACET_EXTENSION_PORTAL_BASE_URL=http://localhost:8000/api/mocks/fep
FEP_GROUP_MODEL_BASE_URL=http://localhost:8000/api/mocks/fep/checkModel

# CBD
CBD_TOKEN_URL=http://localhost:8000/api/mocks/cbd/oauth/token
CBD_API_URL=http://localhost:8000/api/mocks/cbd/coverage
CBD_API=http://localhost:8000/api/mocks/cbd/customer-info
CBD_CLIENT_ID=mock
CBD_CLIENT_SECRET=mock

# Diagnosis
DIAGNOSIS_API_URL=http://localhost:8000/api/mocks/diagnosis

# LINX
LINX_AUTH_URL=http://localhost:8000/api/mocks/linx/oauth/token
LINX_API_URL=http://localhost:8000/api/mocks/linx/claim-search
LINX_CLIENT_ID=mock
LINX_CLIENT_SECRET=mock
LINX_DATASOURCE=mock

# CMS Opt-Out
CMS_API_BASE_URL=http://localhost:8000/api/mocks/cms
CMS_DATASET_ID=opt-out-affidavits

# NPI
NPI_API_BASE_URL=http://localhost:8000/api/mocks/npi/api/

# Behavior toggles
AGENT_TOOLS_SQL_BACKEND=memory       # pymssql-free shim for save_sop_step + cross-prevalence
AGENT_TOOLS_LLM_MOCK=true            # skip real Azure/OpenAI for llm_parse_claim_with_ontology
AGENT_TOOLS_LAZY_LOAD=true           # defer cbd_api_info() and friends until first use
AGENT_TOOLS_HTTP_TIMEOUT=15
```

### Flipping to real upstreams

Swap the eight base URLs for their real values, set the matching
credentials, and disable the toggles you don't want:

```bash
export DOC360_API_BASE=https://doc360.prod.example.com
export DOC360_CLIENT_ID=...; export DOC360_CLIENT_SECRET=...
# ...etc per upstream...
export AGENT_TOOLS_SQL_BACKEND=mssql
export AGENT_TOOLS_LLM_MOCK=false
unset AGENT_TOOLS_LAZY_LOAD          # eager-load for predictable cold paths
```

No code change is required — the tools read these env vars at call time.

### Port mismatch gotcha

If you run Django on a non-default port (e.g. `runserver 127.0.0.1:8009`)
but leave `.env.tools` pointing at `localhost:8000`, the SPA will
successfully POST to `/invoke`, but the tool itself will get
**`Connection refused`** when it tries to reach its mock upstream. Fix it
one of two ways:

```bash
# Option A: run Django on the port .env.tools expects
python manage.py runserver 127.0.0.1:8000

# Option B: override the URLs to match your port before starting Django
export BASE=http://127.0.0.1:8009
export DOC360_API_BASE=$BASE/api/mocks/doc360
export DOC360_TOKEN_URL=$BASE/api/mocks/doc360/security/tokens
export FACETS_BASE_URL=$BASE/api/mocks/facets
export FACET_EXTENSION_PORTAL_BASE_URL=$BASE/api/mocks/fep
export FEP_GROUP_MODEL_BASE_URL=$BASE/api/mocks/fep/checkModel
export CBD_TOKEN_URL=$BASE/api/mocks/cbd/oauth/token
export CBD_API_URL=$BASE/api/mocks/cbd/coverage
export CBD_API=$BASE/api/mocks/cbd/customer-info
export DIAGNOSIS_API_URL=$BASE/api/mocks/diagnosis
export LINX_AUTH_URL=$BASE/api/mocks/linx/oauth/token
export LINX_API_URL=$BASE/api/mocks/linx/claim-search
export CMS_API_BASE_URL=$BASE/api/mocks/cms
export NPI_API_BASE_URL=$BASE/api/mocks/npi/api/
python manage.py runserver 127.0.0.1:8009
```

---

## 7. LangGraph runtime — `single_tool_graph`

A deliberately tiny graph (`agent_tools/graphs/single_tool_graph.py`):

```
START ──► [tool node] ──► END
```

The `tool` node:

1. Looks up the `StructuredTool` from `registry.get_tool(name)`.
2. Calls `tool.invoke(state["args"])`.
3. Returns `{**state, "result": <whatever the tool returned>}`.

If LangGraph isn't installed (older deployments) the runtime falls back to
a direct `tool.invoke(args)` call so the registry stays usable.

This exists so the supervisor graph we'll add later can reuse the same
node wrapper without changing the tool side, and so cross-cutting concerns
(retries, tracing, structured logs) attach in **one** place per tool kind.

The compiled graph is **not** cached across requests — each `run_tool`
call builds a fresh `StateGraph`. That's intentional: the per-call cost is
negligible (< 1 ms) and it keeps the graph thread-safe under Django's
dev-server thread pool.

---

## 8. Data model — Postgres `agent_tools` schema

All three tables live in their own Postgres schema (`agent_tools`),
isolated from `public` to avoid clashes with the legacy `agent_*` tables
and to make grants easy. Django reaches them via the connection-level
`search_path` set in `sop_backend/settings.py`:

```python
"OPTIONS": { "options": "-c search_path=public,agent_tools" }
```

So `db_table` stays as bare names (`tool`, `node_rule_binding`,
`node_tool_binding`) and Postgres resolves them via `search_path`. This
keeps Django's introspection (used by `migrate` and the `TRUNCATE` step
of `TransactionTestCase`) working.

### 8.1 `tool` — the registry

| Column         | Type            | Notes                                                          |
|----------------|-----------------|----------------------------------------------------------------|
| `id`           | uuid PK         |                                                                |
| `name`         | slug, unique    | Same as `StructuredTool.name`, what agents call.               |
| `display_name` | varchar(255)    | UI label.                                                      |
| `description`  | text            | Tool-level description (also the docstring).                   |
| `kind`         | enum            | `langchain` (the 18) or `api_agent` (runtime HTTP agents).     |
| `invoke_url`   | varchar(2048)   | Always `/api/agent-tools/{name}/invoke` for langchain rows.    |
| `args_schema`  | jsonb           | Pydantic-derived JSON Schema for the input.                    |
| `metadata`     | jsonb           | Free-form (tags, return-type hint, per-call defaults).         |
| `endpoint_id`  | varchar(128)    | Only for `kind=api_agent`; FK-like ref into legacy table.      |
| `is_active`    | bool            | Toggle from the admin to hide a tool from the SPA.             |
| `created_at`   | timestamptz     |                                                                |
| `updated_at`   | timestamptz     |                                                                |

Re-seed at any time with `python manage.py sync_tool_registry`.

### 8.2 `node_rule_binding` — replaces `Shape.properties.sop_rules`

One row = "this rule from this SOP is attached to this canvas shape".

Notable columns:

- `shape_id` (FK → `builder.Shape`)
- `sop_id` (FK → `sop_ingestion.AuditSop`)
- `rule_key` — opaque key from `builder.views.attachable`, e.g.
  `pre:{sop_id}:{precondition_id}:{idx}` or
  `step:{sop_id}:{step_number}:{row_index}`.
- `condition`, `action` — auditor-editable overrides.
- `references_json`, `excluded_by_json` — snapshots of the related
  `rule_key`s at attach time.
- `html_reference_json` — the HTML snippet the SPA renders next to the rule.
- Unique on `(shape, rule_key)`.

The full `AttachableSopRule` shape the SPA sees is **reconstituted** by
`GET /api/builder/workflows/:id/attachable` from the live SOP graph —
only fields the auditor edits or overrides on the shape itself are
persisted here.

### 8.3 `node_tool_binding` — replaces `Shape.properties.tool_calls`

One row = "this tool is attached to this canvas shape".

- `shape_id` (FK → `builder.Shape`)
- `tool_id` (FK → `agent_tools.tool`, `ON DELETE PROTECT`)
- `args_template` — pre-fill for the invoke form on this shape.
- `rule_binding_id` (FK → `node_rule_binding`, `ON DELETE SET NULL`) —
  the "tool picked while attaching that rule" relationship; lets the SPA
  group attached tools by rule.
- Unique on `(shape, tool, rule_binding)`.

### 8.4 Round-trip serialization

`Shape.properties` still carries the JSON snapshot the workflow editor
ships back and forth (so the front-end stays in pure-JSON land), but
**the snapshot is rebuilt from the bindings on every save and read**:

- `PUT  /api/builder/workflows/:id/graph` parses
  `properties.sop_rules / tool_calls`, upserts/deletes
  `NodeRuleBinding` / `NodeToolBinding` rows, then writes a normalized
  snapshot back into `properties`.
- `GET  /api/builder/workflows/:id/graph` reads the bindings and
  injects fresh `rule_binding_id` / `tool_id` values back into the
  JSON the SPA expects.

Behaviour and tests live in `test_graph_serialization.py`.

### 8.5 Migrations

| Migration                                | Purpose                                                                 |
|------------------------------------------|-------------------------------------------------------------------------|
| `0000_create_schema`                     | `CREATE SCHEMA IF NOT EXISTS agent_tools` (forward + reverse).          |
| `0001_initial`                           | The three tables + indexes + unique constraints.                        |
| `0002_seed_registry`                     | Calls `registry.sync_to_db()` to seed one row per LangChain tool.       |
| `0003_migrate_shape_properties`          | Reads existing JSON blobs, writes `NodeRuleBinding`/`NodeToolBinding`.  |
| `0004_move_to_agent_tools_schema`        | Brownfield: `ALTER TABLE ... SET SCHEMA agent_tools` + bare `RENAME`. Idempotent. |

Run them with `python manage.py migrate agent_tools`. The migrations are
forward-only safe; reverse migrations are wired but only used in test DBs.

---

## 9. Frontend integration

UI surfaces, in the order an auditor encounters them.

### 9.1 NodePalette (left sidebar) — `ToolRegistryList.tsx`

Calls `GET /api/agent-tools/` on mount. Renders a searchable list of all
active tools with name, `display_name`, and the first line of
`description`. Each row has:

- **Test** — opens `ToolInvokeModal` (see 9.4).
- **Drag handle** — placeholder for future drag-to-canvas.

### 9.2 RulePicker (modal) — `NodeAttachments.tsx`

Triggered by the **+ Attach** button on a canvas node. Has two tabs:

- **Rules** — picked from the SOP graph (`/attachable`).
- **Tools** — same `GET /api/agent-tools/` list as the NodePalette,
  re-used here so the auditor can attach tools **alongside** a rule.

When a tool is checked while a rule is selected, the SPA tracks the
association in `pickedToolToRule` (`Map<toolId, ruleKey>`). On Save,
this becomes a `NodeToolBinding` row with `rule_binding_id` pointing at
the new `NodeRuleBinding`.

### 9.3 ConfigPanel (right side) — `GroupedToolsList`

Per-node display of attached tools, grouped by their `rule_key`:

```
■ Rule: "Verify timely filing window"
    ▸ facets_get_summary            [Run] [Edit args] [×]
    ▸ facets_get_line_details       [Run] [Edit args] [×]
■ Other tools (no rule)
    ▸ save_sop_step                 [Run] [Edit args] [×]
```

The grouping is computed client-side from
`NodeToolBindingSerializer`'s `rule_binding` FK (or "Other" when null).

### 9.4 ToolInvokeModal

A schema-driven form that builds itself from `args_schema`:

- Strings, numbers, booleans → standard inputs.
- Enums → `<select>`.
- Arrays → repeatable rows.
- Nested objects → collapsible group.

On **Run**, POSTs to `invoke_url` with `{ "args": <values> }` and renders
the response (or error) in a pretty-printed panel. The same modal is
re-used from the NodePalette **Test** button and the ConfigPanel **Run**
button — the only difference is the initial `args_template`.

---

## 10. Local development

### 10.1 First-time setup

```bash
cd uhc-backend-v2

# 1. Ensure Postgres is running; the agent_tools schema is created by
#    migration 0000.
python manage.py migrate agent_tools

# 2. (Optional) Re-seed the registry without a migration:
python manage.py sync_tool_registry

# 3. Start the dev server on port 8000 (matches .env.tools defaults).
python manage.py runserver 127.0.0.1:8000
```

### 10.2 Smoke-test all 18 tools

`agent_tools/mock/smoke.py` is a self-contained driver that mints a JWT,
POSTs one canonical request per tool, and prints request + response.

```bash
# JWT_SECRET must be in the environment.
export JWT_SECRET=...   # same secret the Node core-backend uses

python -m agent_tools.mock.smoke                         # all 18
python -m agent_tools.mock.smoke facets_get_summary      # just one
python -m agent_tools.mock.smoke --base http://127.0.0.1:8000   # custom origin
python -m agent_tools.mock.smoke --full                  # full response bodies
```

Expected tail:

```
Done. 18/18 tools returned ok=true.
```

### 10.3 Manual `curl`

```bash
TOKEN=$(python -c 'import os,jwt,time; print(jwt.encode({"sub":"me","role":"ADMIN","iat":int(time.time()),"exp":int(time.time())+3600}, os.environ["JWT_SECRET"], algorithm="HS256"))')

curl -s http://localhost:8000/api/agent-tools/ \
     -H "Authorization: Bearer $TOKEN" | jq '.[].name'

curl -s -X POST http://localhost:8000/api/agent-tools/facets_get_summary/invoke \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"args": {"claim_number": "25XG44660400"}}' | jq
```

### 10.4 Test suite

```bash
python manage.py test agent_tools
```

What's covered:

| Test module                       | Covers                                                              |
|-----------------------------------|---------------------------------------------------------------------|
| `test_models.py`                  | `Tool` upserts, `NodeRuleBinding`/`NodeToolBinding` constraints.    |
| `test_registry_endpoints.py`      | List + detail responses, auth, `tool_kind` discriminator.           |
| `test_tools_end_to_end.py`        | All 18 tools, end-to-end, against the mock server (`LiveServerTestCase`). |
| `test_graph_serialization.py`     | `PUT`/`GET` round-trip of attached rules and tools, FK preservation.|

`test_tools_end_to_end.py` re-seeds the registry inside `setUp` because
`LiveServerTestCase` flushes between methods.

---

## 11. Operational notes

- **Caching.** Facets, CBD and LINX cache their OAuth tokens in
  `agent_tools.tools._cache.ToolCache` (in-process, default 50 min TTL).
  Restart the dev server to invalidate.
- **Logging.** Tool calls log a structured `tool=… upstream=… status=…
  duration_ms=…` line via `agent_tools.tools._logging`. Errors are
  re-raised with the upstream URL in the message so 5xx responses are
  immediately diagnosable.
- **Lazy imports.** Both the LangGraph runtime (`run_tool`) and the
  underlying tool builders are imported lazily so a missing optional
  dep (e.g. `pymssql`) only affects the affected tool, not the registry.
- **`api_agent` rows.** The runtime HTTP-agent flow creates `Tool` rows
  with `kind='api_agent'` and a populated `endpoint_id`. The SPA renders
  them next to the langchain rows; on attach, a `NodeToolBinding` row is
  written exactly the same way as for langchain tools, so any consumer
  that follows the binding doesn't need to special-case the kind.
- **Admin.** `agent_tools/admin.py` registers all three models; use it
  to toggle `is_active`, inspect bindings, and verify seed runs.

---

## 12. Where to look next

- `docs/STORAGE.md` — how SOP data is laid out across Postgres, Neo4j,
  Redis, and Mongo.
- `extracted-tools-main/docs/TOOLS.md` — per-tool field-level request /
  response / error schemas.
- `agent_tools/mock/smoke.py` — canonical request example per tool.
- `agent_tools/tests/test_tools_end_to_end.py` — copy-paste-friendly
  invocation examples that already pass against the mock server.
