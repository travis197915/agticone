"""APICallerAgent — executes the HTTP request and captures the raw response.

Honours `use_cache=True` by short-circuiting from the Redis response cache
when the same endpoint was called recently.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ..store import AuthSpec, CredentialStore

if TYPE_CHECKING:
    from ..state import AgentState
    from ..config import AgentConfig


def api_caller(state: "AgentState", cfg: "AgentConfig") -> dict:
    if state.get("error"):
        return {}

    method = state["method"]
    url    = state["url"]
    auth   = AuthSpec.from_dict(state.get("resolved_auth") or {})

    store = CredentialStore(cfg)

    # Optional Redis cache hit
    if state.get("use_cache") and method == "GET":
        cached = store.get_cached_response(state["endpoint_id"])
        if cached is not None:
            return {
                "status_code": 200,
                "duration_ms": 0,
                "response_bytes": 0,
                "response_text": "",
                "is_json": True,
                "json": cached,
                "success": True,
                "cache_hit": True,
                "stages": [{
                    "agent": "APICallerAgent",
                    "status": "OK",
                    "msg": f"cache hit endpoint={state['endpoint_id']}",
                }],
            }

    # Build the request
    import requests
    headers = dict(state.get("request_headers") or {})
    headers.update(auth.to_request_headers())
    headers.setdefault("User-Agent", "uhc-api-agent/1.0")

    params  = state.get("request_query") or None
    body    = state.get("body")

    json_body = None
    data_body = None
    if isinstance(body, (dict, list)):
        json_body = body
        headers.setdefault("Content-Type", "application/json")
    elif body is not None:
        data_body = body

    started = time.time()
    try:
        resp = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_body,
            data=data_body,
            auth=auth.basic_auth_tuple(),
            timeout=cfg.http_timeout,
            stream=True,
        )
    except requests.RequestException as e:
        return {
            "success": False,
            "error": f"http error: {e}",
            "duration_ms": int((time.time() - started) * 1000),
            "stages": [{"agent": "APICallerAgent", "status": "ERROR", "msg": str(e)}],
        }

    # Stream-read with a size guard
    chunks: list[bytes] = []
    total = 0
    cap = cfg.max_response_bytes
    try:
        for chunk in resp.iter_content(65536):
            total += len(chunk)
            if total > cap:
                resp.close()
                return {
                    "status_code": resp.status_code,
                    "success": False,
                    "error": f"response >{cap} bytes",
                    "duration_ms": int((time.time() - started) * 1000),
                    "response_bytes": total,
                    "stages": [{"agent": "APICallerAgent", "status": "ERROR", "msg": "size cap"}],
                }
            chunks.append(chunk)
        raw = b"".join(chunks)
    except Exception as e:
        return {
            "status_code": resp.status_code,
            "success": False,
            "error": f"read error: {e}",
            "duration_ms": int((time.time() - started) * 1000),
            "stages": [{"agent": "APICallerAgent", "status": "ERROR", "msg": str(e)}],
        }

    duration_ms = int((time.time() - started) * 1000)
    text = raw.decode(resp.encoding or "utf-8", errors="replace")
    success = 200 <= resp.status_code < 400

    return {
        "status_code":    resp.status_code,
        "duration_ms":    duration_ms,
        "response_bytes": len(raw),
        "response_text":  text,
        "success":        success,
        "cache_hit":      False,
        "request_headers": {k: v for k, v in headers.items() if k.lower() != "authorization"},
        "stages": [{
            "agent": "APICallerAgent",
            "status": "OK" if success else "ERROR",
            "msg": f"HTTP {resp.status_code} {duration_ms}ms {len(raw)}B",
        }],
    }
