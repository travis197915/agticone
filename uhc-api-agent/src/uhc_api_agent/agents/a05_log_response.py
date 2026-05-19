"""ResponseLoggerAgent — writes the call to Postgres, archives in Mongo, caches in Redis."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..store import CredentialStore

if TYPE_CHECKING:
    from ..state import AgentState
    from ..config import AgentConfig


def response_logger(state: "AgentState", cfg: "AgentConfig") -> dict:
    store = CredentialStore(cfg)

    success = bool(state.get("success"))
    call_id = state["call_id"]
    endpoint_id = state.get("endpoint_id") or ""

    # Only attach the FK if the endpoint is actually registered — otherwise log
    # the call with endpoint_id=NULL (anonymous one-off call).
    fk_endpoint_id = None
    if endpoint_id:
        try:
            if store.load_endpoint(state.get("method", "GET"), state.get("url", "")):
                fk_endpoint_id = endpoint_id
        except Exception:
            fk_endpoint_id = None

    # 1) Postgres call log (always runs, even on failure)
    try:
        store.log_call(
            call_id=call_id,
            endpoint_id=fk_endpoint_id,
            method=state.get("method", "GET"),
            url=state.get("url", ""),
            status_code=state.get("status_code"),
            duration_ms=int(state.get("duration_ms") or 0),
            response_bytes=int(state.get("response_bytes") or 0),
            is_json=bool(state.get("is_json")),
            success=success,
            error_message=state.get("error", "") or "",
        )
    except Exception as e:
        return {
            "stages": [{"agent": "ResponseLoggerAgent", "status": "ERROR", "msg": f"pg log failed: {e}"}],
        }

    # 2) Mongo archive (best effort, only on cache-miss to avoid double writes)
    if not state.get("cache_hit"):
        store.archive_response(
            call_id=call_id,
            payload={
                "call_id":       call_id,
                "endpoint_id":   endpoint_id,
                "method":        state.get("method"),
                "url":           state.get("url"),
                "status_code":   state.get("status_code"),
                "duration_ms":   state.get("duration_ms"),
                "response_bytes": state.get("response_bytes"),
                "is_json":       state.get("is_json"),
                "success":       success,
                "json":          state.get("json"),
                "response_text": state.get("response_text") if not state.get("is_json") else None,
                "error":         state.get("error", ""),
                "request_headers": state.get("request_headers"),
                "request_query":   state.get("request_query"),
            },
        )

    # 3) Redis response cache (only successful JSON GETs)
    if success and state.get("is_json") and (state.get("method", "GET") == "GET") \
       and endpoint_id and not state.get("cache_hit"):
        store.cache_response(endpoint_id=endpoint_id, json_body=state.get("json"))

    return {
        "stages": [{
            "agent": "ResponseLoggerAgent",
            "status": "OK",
            "msg": f"logged call_id={call_id} success={success}",
        }],
    }
