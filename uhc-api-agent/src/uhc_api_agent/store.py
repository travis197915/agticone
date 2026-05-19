"""Persistence layer for the API agent.

POSTGRES — system of record
    api_agent_endpoints   one row per (method, url): auth_type + auth payload + headers
    api_agent_call_log    one row per API call: status, latency, body size, error

MONGO     — raw response archive
    api_agent_responses   one document per call with the full request + parsed JSON

REDIS     — short-lived caches
    apiagent:endpoint:<sha>      endpoint config (TTL 24h, refreshed on every load)
    apiagent:response:<sha>      last successful JSON response (TTL = cfg.cache_ttl)

`<sha>` = sha256(method + " " + url)[:32]. Auth secrets are stored only in Postgres
and Redis caches the response JSON, never the auth.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .config import AgentConfig, get_pg, get_mongo, get_redis


# ── Auth models ───────────────────────────────────────────────────────────────

AUTH_TYPES = {"none", "bearer", "basic", "api_key", "custom"}


@dataclass
class AuthSpec:
    """Auth payload stored per endpoint.

    type        : one of AUTH_TYPES
    token       : bearer token (when type=bearer)
    username    : basic auth user  (when type=basic)
    password    : basic auth pass  (when type=basic)
    api_key     : api key value    (when type=api_key)
    header_name : header for api_key, default "Authorization"
    headers     : free-form extra headers merged on every call (when type=custom or any)
    """
    type: str = "none"
    token: str = ""
    username: str = ""
    password: str = ""
    api_key: str = ""
    header_name: str = "Authorization"
    headers: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.type not in AUTH_TYPES:
            raise ValueError(f"Unknown auth type {self.type!r}. "
                             f"Allowed: {sorted(AUTH_TYPES)}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "AuthSpec":
        if not d:
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_request_headers(self) -> dict:
        """Materialise the auth into requests-compatible headers."""
        out: dict = dict(self.headers or {})
        if self.type == "bearer" and self.token:
            out.setdefault("Authorization", f"Bearer {self.token}")
        elif self.type == "api_key" and self.api_key:
            out.setdefault(self.header_name or "Authorization", self.api_key)
        return out

    def basic_auth_tuple(self) -> Optional[tuple[str, str]]:
        if self.type == "basic" and (self.username or self.password):
            return (self.username, self.password)
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def endpoint_key(method: str, url: str) -> str:
    h = hashlib.sha256(f"{method.upper()} {url}".encode()).hexdigest()
    return h[:32]


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS api_agent_endpoints (
    endpoint_id   TEXT PRIMARY KEY,
    name          TEXT,
    method        TEXT NOT NULL,
    url           TEXT NOT NULL,
    auth_type     TEXT NOT NULL DEFAULT 'none',
    auth_payload  JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_headers JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_query   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    call_count    INTEGER NOT NULL DEFAULT 0,
    last_called_at TIMESTAMPTZ,
    UNIQUE (method, url)
);

CREATE INDEX IF NOT EXISTS idx_api_agent_endpoints_url ON api_agent_endpoints (url);

CREATE TABLE IF NOT EXISTS api_agent_call_log (
    id            BIGSERIAL PRIMARY KEY,
    call_id       UUID NOT NULL,
    endpoint_id   TEXT REFERENCES api_agent_endpoints(endpoint_id) ON DELETE SET NULL,
    method        TEXT NOT NULL,
    url           TEXT NOT NULL,
    status_code   INTEGER,
    duration_ms   INTEGER,
    response_bytes INTEGER,
    is_json       BOOLEAN NOT NULL DEFAULT FALSE,
    success       BOOLEAN NOT NULL DEFAULT FALSE,
    error_message TEXT,
    called_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_api_agent_call_log_endpoint ON api_agent_call_log (endpoint_id);
CREATE INDEX IF NOT EXISTS idx_api_agent_call_log_called_at ON api_agent_call_log (called_at DESC);
"""


class CredentialStore:
    """All persistence — endpoints + auth + history + cache."""

    REDIS_ENDPOINT_TTL = 86400      # 24h
    MONGO_COLLECTION   = "api_agent_responses"

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self._ensure_schema()

    # ── schema ────────────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        conn = get_pg(self.cfg)
        with conn.cursor() as cur:
            cur.execute(_DDL)

    # ── endpoint registry ─────────────────────────────────────────────────────

    def save_endpoint(
        self,
        method: str,
        url: str,
        auth: AuthSpec,
        *,
        name: str = "",
        default_headers: dict | None = None,
        default_query: dict | None = None,
    ) -> str:
        """Insert or update an endpoint config. Returns endpoint_id."""
        eid = endpoint_key(method, url)
        conn = get_pg(self.cfg)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_agent_endpoints
                    (endpoint_id, name, method, url, auth_type, auth_payload,
                     default_headers, default_query)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
                ON CONFLICT (method, url) DO UPDATE SET
                    name = EXCLUDED.name,
                    auth_type = EXCLUDED.auth_type,
                    auth_payload = EXCLUDED.auth_payload,
                    default_headers = EXCLUDED.default_headers,
                    default_query = EXCLUDED.default_query,
                    updated_at = NOW()
                """,
                (
                    eid,
                    name or "",
                    method.upper(),
                    url,
                    auth.type,
                    json.dumps(auth.to_dict()),
                    json.dumps(default_headers or {}),
                    json.dumps(default_query or {}),
                ),
            )
        # cache in Redis too
        try:
            r = get_redis(self.cfg)
            r.setex(
                f"apiagent:endpoint:{eid}",
                self.REDIS_ENDPOINT_TTL,
                json.dumps({
                    "endpoint_id": eid,
                    "name": name,
                    "method": method.upper(),
                    "url": url,
                    "auth": auth.to_dict(),
                    "default_headers": default_headers or {},
                    "default_query": default_query or {},
                }),
            )
        except Exception:
            pass
        return eid

    def load_endpoint(self, method: str, url: str) -> Optional[dict]:
        """Look up an endpoint config — Redis first, Postgres fallback."""
        eid = endpoint_key(method, url)

        # Redis fast path
        try:
            r = get_redis(self.cfg)
            cached = r.get(f"apiagent:endpoint:{eid}")
            if cached:
                return json.loads(cached)
        except Exception:
            pass

        # Postgres source-of-truth
        conn = get_pg(self.cfg)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT endpoint_id, name, method, url, auth_type, auth_payload,
                       default_headers, default_query, call_count, last_called_at
                FROM   api_agent_endpoints
                WHERE  method = %s AND url = %s
                """,
                (method.upper(), url),
            )
            row = cur.fetchone()
        if not row:
            return None
        rec = {
            "endpoint_id":    row[0],
            "name":           row[1] or "",
            "method":         row[2],
            "url":            row[3],
            "auth":           row[5] or {"type": row[4] or "none"},
            "default_headers": row[6] or {},
            "default_query":   row[7] or {},
            "call_count":     row[8] or 0,
            "last_called_at": row[9].isoformat() if row[9] else None,
        }

        # warm Redis cache
        try:
            r = get_redis(self.cfg)
            r.setex(f"apiagent:endpoint:{eid}", self.REDIS_ENDPOINT_TTL, json.dumps(rec))
        except Exception:
            pass
        return rec

    def list_endpoints(self) -> list[dict]:
        conn = get_pg(self.cfg)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT endpoint_id, name, method, url, auth_type, call_count,
                       last_called_at, created_at, updated_at
                FROM   api_agent_endpoints
                ORDER  BY updated_at DESC
                """
            )
            rows = cur.fetchall()
        return [
            {
                "endpoint_id":    r[0],
                "name":           r[1] or "",
                "method":         r[2],
                "url":            r[3],
                "auth_type":      r[4],
                "call_count":     r[5],
                "last_called_at": r[6].isoformat() if r[6] else None,
                "created_at":     r[7].isoformat() if r[7] else None,
                "updated_at":     r[8].isoformat() if r[8] else None,
            }
            for r in rows
        ]

    def delete_endpoint(self, method: str, url: str) -> bool:
        eid = endpoint_key(method, url)
        conn = get_pg(self.cfg)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM api_agent_endpoints WHERE endpoint_id = %s",
                (eid,),
            )
            deleted = cur.rowcount > 0
        try:
            r = get_redis(self.cfg)
            r.delete(f"apiagent:endpoint:{eid}")
            r.delete(f"apiagent:response:{eid}")
        except Exception:
            pass
        return deleted

    # ── call history ──────────────────────────────────────────────────────────

    def log_call(
        self,
        *,
        call_id: str,
        endpoint_id: Optional[str],
        method: str,
        url: str,
        status_code: Optional[int],
        duration_ms: int,
        response_bytes: int,
        is_json: bool,
        success: bool,
        error_message: str = "",
    ) -> None:
        conn = get_pg(self.cfg)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_agent_call_log
                    (call_id, endpoint_id, method, url, status_code, duration_ms,
                     response_bytes, is_json, success, error_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (call_id, endpoint_id, method.upper(), url, status_code,
                 duration_ms, response_bytes, is_json, success, error_message[:2000]),
            )
            if success and endpoint_id:
                cur.execute(
                    """
                    UPDATE api_agent_endpoints
                       SET call_count = call_count + 1,
                           last_called_at = NOW()
                     WHERE endpoint_id = %s
                    """,
                    (endpoint_id,),
                )

    def list_history(self, *, url: str = "", limit: int = 50) -> list[dict]:
        conn = get_pg(self.cfg)
        with conn.cursor() as cur:
            if url:
                cur.execute(
                    """
                    SELECT call_id, method, url, status_code, duration_ms,
                           response_bytes, is_json, success, error_message, called_at
                    FROM   api_agent_call_log
                    WHERE  url = %s
                    ORDER  BY called_at DESC
                    LIMIT  %s
                    """,
                    (url, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT call_id, method, url, status_code, duration_ms,
                           response_bytes, is_json, success, error_message, called_at
                    FROM   api_agent_call_log
                    ORDER  BY called_at DESC
                    LIMIT  %s
                    """,
                    (limit,),
                )
            rows = cur.fetchall()
        return [
            {
                "call_id":        str(r[0]),
                "method":         r[1],
                "url":            r[2],
                "status_code":    r[3],
                "duration_ms":    r[4],
                "response_bytes": r[5],
                "is_json":        r[6],
                "success":        r[7],
                "error_message":  r[8] or "",
                "called_at":      r[9].isoformat() if r[9] else None,
            }
            for r in rows
        ]

    # ── Mongo raw-response archive ────────────────────────────────────────────

    def archive_response(self, *, call_id: str, payload: dict) -> None:
        try:
            db = get_mongo(self.cfg)[self.cfg.mongo_database]
            doc = dict(payload)
            doc["_id"] = call_id
            doc.setdefault("archived_at", time.time())
            db[self.MONGO_COLLECTION].replace_one({"_id": call_id}, doc, upsert=True)
        except Exception:
            pass  # archive is best-effort

    def load_response(self, call_id: str) -> Optional[dict]:
        try:
            db = get_mongo(self.cfg)[self.cfg.mongo_database]
            return db[self.MONGO_COLLECTION].find_one({"_id": call_id})
        except Exception:
            return None

    # ── Redis response cache ──────────────────────────────────────────────────

    def cache_response(self, *, endpoint_id: str, json_body: Any) -> None:
        try:
            r = get_redis(self.cfg)
            r.setex(
                f"apiagent:response:{endpoint_id}",
                self.cfg.cache_ttl,
                json.dumps(json_body, default=str),
            )
        except Exception:
            pass

    def get_cached_response(self, endpoint_id: str) -> Optional[Any]:
        try:
            r = get_redis(self.cfg)
            cached = r.get(f"apiagent:response:{endpoint_id}")
            return json.loads(cached) if cached else None
        except Exception:
            return None


__all__ = ["AuthSpec", "CredentialStore", "endpoint_key", "AUTH_TYPES"]
