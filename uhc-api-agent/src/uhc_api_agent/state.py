"""LangGraph state schema for the API-calling agent."""
from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    # ── inputs ────────────────────────────────────────────────────────────────
    url:        str
    method:     str                    # GET, POST, PUT, PATCH, DELETE
    body:       Any                    # dict → sent as JSON; str → raw body
    query:      dict                   # query-string params
    headers:    dict                   # one-off headers merged on top of stored headers
    auth_input: dict                   # raw auth payload supplied at call time
    name:       str                    # friendly name to store with the endpoint
    save_auth:  bool                   # explicit "register this URL/auth" flag
    use_cache:  bool                   # read from Redis response cache first

    # ── resolved ─────────────────────────────────────────────────────────────
    call_id:        str                # uuid per call — links Postgres ↔ Mongo
    endpoint_id:    str                # sha256 of method+url
    resolved_auth:  dict               # AuthSpec dict actually used for the request
    request_headers: dict
    request_query:   dict

    # ── outputs ───────────────────────────────────────────────────────────────
    status_code:    int
    duration_ms:    int
    response_bytes: int
    response_text:  str
    is_json:        bool
    json:           Any                # parsed JSON or None
    success:        bool
    error:          str                # populated only on failure
    cache_hit:      bool

    # ── audit ─────────────────────────────────────────────────────────────────
    stages: list[dict]                 # [{agent, status, msg}] per node
