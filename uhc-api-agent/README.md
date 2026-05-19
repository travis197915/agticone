# uhc-api-agent

Standalone LangGraph agent. **URL in, JSON out.** Persists the URL and auth
the first time you call it; every subsequent call to the same URL Just Works
without re-supplying credentials.

This package is **independent** of `uhc-sop-ingestion`. It is its own
LangGraph, its own CLI (`api-agent`), and its own set of Postgres /
Mongo / Redis tables. It only **shares the same `.env`** so you don't have to
reconfigure the existing databases.

---

## Storage layout

All three existing databases are reused — **no SQLite, no new infra**.

| Store    | What lives there                                                                   | Tables / keys                                          |
| -------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------ |
| Postgres | Endpoint registry + auth + full call log (system of record)                        | `api_agent_endpoints`, `api_agent_call_log`            |
| MongoDB  | Raw archived JSON / text body for every call                                       | `api_agent_responses` (collection in `MONGO_DATABASE`) |
| Redis    | Hot caches: endpoint config (24 h) and last successful JSON response (`CACHE_TTL`) | `apiagent:endpoint:<sha>`, `apiagent:response:<sha>`   |

Schema is created automatically the first time the agent runs (`CREATE TABLE IF NOT EXISTS`).

---

## The pipeline (5 atomic agents)

```
validate_url → resolve_auth → api_caller → json_parser → response_logger
```

| Agent                 | Job                                                                              |
| --------------------- | -------------------------------------------------------------------------------- |
| `URLValidatorAgent`   | Sanitises URL + HTTP method, assigns a `call_id` UUID.                           |
| `AuthResolverAgent`   | If auth was supplied → store it against the URL. Else → load whatever is stored. |
| `APICallerAgent`      | Executes the request, honours `use_cache` for GETs.                              |
| `JSONParserAgent`     | Parses the body as JSON; tolerates non-JSON bodies.                              |
| `ResponseLoggerAgent` | Postgres call log + Mongo body archive + Redis response cache.                   |

---

## Install

```bash
cd uhc-backend-v2
pip install -e ./uhc-api-agent
```

The same `.env` at the repo root is auto-discovered.

Optional env-only knobs:

```
API_AGENT_TIMEOUT=30          # seconds per HTTP request
API_AGENT_CACHE_TTL=300       # seconds the JSON response stays in Redis
API_AGENT_MAX_BYTES=52428800  # 50 MiB body cap
```

---

## CLI

```bash
# First call — supplies auth; URL + auth are saved automatically
api-agent call https://api.example.com/v1/users --bearer sk-xyz

# Second call — auth is reused, no need to repeat it
api-agent call https://api.example.com/v1/users

# Other auth modes
api-agent call https://api.example.com/v1/users --basic alice:s3cret
api-agent call https://api.example.com/v1/users \
    --api-key abc123 --api-key-header X-API-Key
api-agent call https://api.example.com/v1/users \
    --header "X-Tenant: acme" --header "X-Trace: yes"

# POST with a JSON body
api-agent call https://api.example.com/v1/login \
    --method POST \
    --json '{"username":"alice","password":"s3cret"}'

# Save credentials without calling
api-agent register https://api.example.com/v1/users \
    --bearer sk-xyz --name "User list"

# Inspect & manage
api-agent endpoints
api-agent show    https://api.example.com/v1/users          # masks secrets
api-agent show    https://api.example.com/v1/users --reveal # shows secrets
api-agent history --url https://api.example.com/v1/users --limit 20
api-agent delete  https://api.example.com/v1/users
```

Exit code is `0` on a successful (`2xx/3xx`) response, `2` otherwise.

---

## Python API

```python
from uhc_api_agent import ApiAgentPipeline

pipeline = ApiAgentPipeline()

# First call: pass auth — it's persisted against the URL
pipeline.run(
    url="https://api.example.com/v1/me",
    auth={"type": "bearer", "token": "sk-xyz"},
)

# Subsequent calls: auth is loaded automatically
out = pipeline.run(url="https://api.example.com/v1/me")
print(out["status_code"], out["json"])

# POST with a body
pipeline.run(
    url="https://api.example.com/v1/login",
    method="POST",
    body={"username": "alice", "password": "s3cret"},
)

# Register without calling
pipeline.register(
    url="https://api.example.com/v1/users",
    auth={"type": "api_key", "api_key": "abc", "header_name": "X-API-Key"},
)

# Browse what's stored
pipeline.list_endpoints()
pipeline.history(url="https://api.example.com/v1/users", limit=10)
pipeline.delete_endpoint(url="https://api.example.com/v1/users")
```

The dict returned by `pipeline.run(...)` contains:

```
{
  "call_id":       "<uuid>",
  "endpoint_id":   "<sha256[:32]>",
  "method":        "GET",
  "url":           "...",
  "status_code":   200,
  "duration_ms":   123,
  "response_bytes": 4567,
  "is_json":       true,
  "json":          { ... },
  "response_text": null,           # only set when is_json=false
  "success":       true,
  "cache_hit":     false,
  "error":         "",
  "stages": [
    {"agent": "URLValidatorAgent",  "status": "OK", "msg": "..."},
    {"agent": "AuthResolverAgent",  "status": "OK", "msg": "..."},
    {"agent": "APICallerAgent",     "status": "OK", "msg": "HTTP 200 123ms 4567B"},
    {"agent": "JSONParserAgent",    "status": "OK", "msg": "parsed type=dict"},
    {"agent": "ResponseLoggerAgent","status": "OK", "msg": "logged call_id=..."}
  ]
}
```

---

## Supported auth modes

| `type`    | Required fields                   | Behaviour                                                         |
| --------- | --------------------------------- | ----------------------------------------------------------------- |
| `none`    | —                                 | No auth. Custom headers (if any) still applied.                   |
| `bearer`  | `token`                           | Adds `Authorization: Bearer <token>`.                             |
| `basic`   | `username`, `password`            | Standard HTTP Basic auth.                                         |
| `api_key` | `api_key`, optional `header_name` | Adds `<header_name>: <api_key>` (default header `Authorization`). |
| `custom`  | `headers` (dict)                  | Free-form headers, e.g. signed requests.                          |

Headers passed via `headers={…}` (or `--header`) are merged on top of any
defaults stored with the endpoint, on every call.

---

pip install -e ./uhc-api-agent

api-agent call https://api.example.com/v1/users --bearer sk-xyz # saves
api-agent call https://api.example.com/v1/users # reuses
api-agent register https://api.example.com/v1/x --api-key abc --api-key-header X-API-Key
api-agent endpoints
api-agent show https://... [--reveal]
api-agent history --url https://... --limit 20
api-agent delete https://...

## Inspecting the data directly

```sql
-- Recent calls
SELECT method, url, status_code, duration_ms, success, called_at
FROM   api_agent_call_log
ORDER  BY called_at DESC LIMIT 20;

-- Endpoint registry (auth payload is JSONB)
SELECT method, url, auth_type, auth_payload, call_count, last_called_at
FROM   api_agent_endpoints;
```

```js
// Mongo: raw body archive
use sop_ingestion
db.api_agent_responses.find({}, {url:1, status_code:1, success:1}).sort({archived_at:-1}).limit(20)
```

```bash
# Redis: cached responses
redis-cli KEYS 'apiagent:*'
redis-cli GET  apiagent:response:<endpoint_id>
```
