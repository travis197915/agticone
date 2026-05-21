# Runtime API agent (`uhc-api-agent`)

A standalone LangGraph package that lets a workflow node make an authenticated HTTP call at claim-evaluation time.

The package is editable-installed by `pip install -e ./uhc-api-agent` and lives in [uhc-api-agent/](../uhc-api-agent/).

---

## 1. Why it exists

The ingestion pipeline ingests SOPs into a knowledge graph. The **builder** lets an auditor pick rules from that graph and pin them to nodes. But many real-world steps also need live data — eligibility lookups, claims-system queries, deny-code translators, etc.

A *runtime agent* is exactly that: a registered HTTP endpoint that a workflow node calls per-claim. The package handles:

* Endpoint registration (URL + method + auth).
* Credential storage (Postgres).
* HTTP execution with timing + status capture.
* JSON parsing.
* Audit log (one row in Postgres + one document in Mongo per call).
* Optional Redis response cache.

---

## 2. Public API

```python
from uhc_api_agent import ApiAgentPipeline

pipeline = ApiAgentPipeline()           # auto-loads .env

# First call: register the endpoint + run it.
result = pipeline.run(
    url="https://api.example.com/v1/eligibility",
    method="POST",
    body={"member_id": "M123"},
    auth={"type": "bearer", "token": "sk-xyz"},
    save_auth=True,
)
print(result["status_code"], result["json"])
# 200 {'eligible': True, 'plan': 'Commercial'}

# Subsequent calls: stored auth is reused.
result = pipeline.run(
    url="https://api.example.com/v1/eligibility",
    method="POST",
    body={"member_id": "M456"},
)
```

Source: [pipeline.py](../uhc-api-agent/src/uhc_api_agent/pipeline.py).

### Methods

| Method                          | Purpose                                             |
|---------------------------------|-----------------------------------------------------|
| `pipeline.run(url, …)`          | Call an endpoint. Returns a dict (see §4).          |
| `pipeline.register(url, …)`     | Save endpoint + auth without calling. Returns `endpoint_id`. |
| `pipeline.list_endpoints()`     | List every registered endpoint.                     |
| `pipeline.history(url=, limit=)`| Audit log of recent calls.                          |
| `pipeline.delete_endpoint(url, method=)` | Forget a registered endpoint.              |

---

## 3. LangGraph flow

Linear, 5 nodes (see [graph.py](../uhc-api-agent/src/uhc_api_agent/graph.py)):

```
START → validate_url → resolve_auth → api_caller → json_parser → response_logger → END
```

### `validate_url` ([a01_validate_url.py](../uhc-api-agent/src/uhc_api_agent/agents/a01_validate_url.py))

Confirms the URL has a scheme + host. Generates a `call_id` (UUID) and an `endpoint_id` (sha256 of `method + url`).

### `resolve_auth` ([a02_resolve_auth.py](../uhc-api-agent/src/uhc_api_agent/agents/a02_resolve_auth.py))

If the caller supplied `auth=`, that wins.  Otherwise looks up the stored `AuthSpec` by `endpoint_id` from `api_agent_endpoints`. Populates `request_headers` (adds `Authorization: Bearer …`, `X-API-Key: …`, or `Authorization: Basic …` depending on type).

### `api_caller` ([a03_api_caller.py](../uhc-api-agent/src/uhc_api_agent/agents/a03_api_caller.py))

Executes the HTTP call via `requests`. Captures `status_code`, `duration_ms`, raw `response_text`, response headers. 30-second default timeout. On exception, records `error` and `success=false` but does **not** raise — downstream nodes still get to log.

### `json_parser` ([a04_parse_json.py](../uhc-api-agent/src/uhc_api_agent/agents/a04_parse_json.py))

Attempts `json.loads(response_text)`. Sets `is_json=True` + populates `json` field; otherwise leaves `json=None`.

### `response_logger` ([a05_log_response.py](../uhc-api-agent/src/uhc_api_agent/agents/a05_log_response.py))

* One row into `api_agent_call_history` (Postgres) — `call_id`, `endpoint_id`, `status_code`, `duration_ms`, `success`, `error`, timestamp.
* One document into Mongo `api_agent_responses` collection — `_id = call_id`, full body, full headers.
* If `use_cache=True` was passed and the call succeeded, the response is also cached in Redis under `api_call:cache:<endpoint_id>:<sha256(body)>` with a 5-minute TTL.

---

## 4. Return shape

```python
{
    "call_id":       "a3b9-…",            # UUID, links Postgres ↔ Mongo
    "endpoint_id":   "ep_…",
    "method":        "POST",
    "url":           "https://...",
    "status_code":   200,
    "duration_ms":   124,
    "response_bytes": 4096,
    "is_json":       True,
    "json":          {"eligible": True},  # parsed JSON or None
    "response_text": None,                # only populated when is_json=False
    "success":       True,
    "cache_hit":     False,
    "error":         "",
    "stages": [                           # audit trail per LangGraph node
        {"agent": "url_validator",  "status": "ok"},
        {"agent": "auth_resolver",  "status": "ok"},
        {"agent": "api_caller",     "status": "ok", "ms": 124},
        {"agent": "json_parser",    "status": "ok"},
        {"agent": "response_logger","status": "ok"}
    ]
}
```

Secrets (`resolved_auth`) are deliberately omitted from the return shape.

---

## 5. Credential store

[store.py](../uhc-api-agent/src/uhc_api_agent/store.py) exposes the `CredentialStore` and `AuthSpec` types.

### `AuthSpec`

```python
@dataclass
class AuthSpec:
    type: Literal["none", "bearer", "api_key", "basic"]
    token: str | None       = None     # for bearer
    api_key: str | None     = None     # for api_key
    header_name: str        = "Authorization"   # for api_key
    username: str | None    = None     # for basic
    password: str | None    = None     # for basic
```

`AuthSpec.from_dict(...)` parses the auth dict accepted by `pipeline.run(auth=)`.

### Postgres tables

The package writes its own tables (not Django-managed; created on first use):

| Table                       | Columns                                                                 |
|-----------------------------|-------------------------------------------------------------------------|
| `api_agent_endpoints`       | `endpoint_id`, `method`, `url`, `name`, `auth_type`, `auth_json`, `default_headers`, `default_query`, `created_at`. |
| `api_agent_call_history`    | `call_id`, `endpoint_id`, `method`, `url`, `status_code`, `duration_ms`, `response_bytes`, `is_json`, `success`, `error`, `called_at`. |

`auth_json` is JSONB. Tokens are stored as plaintext — protect access to this database accordingly. (No field-level encryption today.)

---

## 6. Integration with the builder

When a workflow is created (or `POST /api/builder/workflows/<id>/attach/` is called) with `runtime_agents[]`, [attachments.py](../builder/attachments.py) does this for each agent:

```python
from uhc_api_agent import ApiAgentPipeline
pipeline = ApiAgentPipeline(env_path=BASE_DIR / ".env")

endpoint_id = pipeline.register(
    url=agent["url"],
    method=agent["method"],
    auth=_make_auth_payload(agent),    # turns the form's fields into AuthSpec
    name=agent.get("name"),
)
```

The returned `endpoint_id` is stored on `Workflow.metadata.runtime_agents[i].endpoint_id`. The plaintext `auth_token` from the request is **stripped** before persistence — only `api_agent_endpoints.auth_json` has the credential.

When the SPA's per-node dialog renders "Tool Calls", it iterates `Workflow.metadata.runtime_agents` and emits one entry per `endpoint_id`. The auditor's pick is stored on `Shape.properties.tool_calls = ["agent:ep_…"]`.

At claim-evaluation time, the runtime executor (not in this repo) would:

1. Read `Shape.properties.tool_calls`.
2. Look up `endpoint_id` → `api_agent_endpoints` row.
3. Call `pipeline.run(url=…)` — stored auth is reused.
4. Inspect `result["json"]` for downstream branching.

---

## 7. Environment

Same `.env` as the Django project:

```
PG_HOST=…
PG_PORT=…
PG_USER=…
PG_PASSWORD=…
PG_DATABASE=…
REDIS_HOST=…
REDIS_PORT=…
REDIS_USER=…
REDIS_PASSWORD=…
MONGO_HOST=…
MONGO_PORT=…
MONGO_USER=…
MONGO_PASSWORD=…
MONGO_DATABASE=…
```

`AgentConfig.from_env(env_path)` ([config.py](../uhc-api-agent/src/uhc_api_agent/config.py)) reads these and exposes typed accessors.

---

## 8. CLI

[cli.py](../uhc-api-agent/src/uhc_api_agent/cli.py) installs an `uhc-api-agent` console script with subcommands `call`, `register`, `list`, `history`, `delete`.

```bash
# One-off call
uhc-api-agent call https://httpbin.org/get

# Register with auth then call by URL only
uhc-api-agent register https://api.example.com/v1/me \
    --auth bearer --token sk-xyz
uhc-api-agent call https://api.example.com/v1/me

# Inspect what's stored
uhc-api-agent list
uhc-api-agent history --url https://api.example.com/v1/me
```

---

## 9. Failure modes

* **Network error / timeout** — `status_code=0`, `success=false`, `error` populated. A row is still written to `api_agent_call_history`.
* **Non-JSON response** — `is_json=false`, `json=None`, `response_text` returned (truncated for state, but full text persists to Mongo).
* **Bad credentials** — the upstream API typically returns 401/403; the call is logged as `success=false` with the upstream status code. The stored `auth_json` is not modified — fix it via `pipeline.register(...)` to overwrite.
* **Mongo / Postgres down** — `response_logger` swallows storage errors so the caller still gets the HTTP result. The audit trail is best-effort.
