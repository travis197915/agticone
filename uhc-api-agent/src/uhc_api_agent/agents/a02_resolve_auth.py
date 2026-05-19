"""AuthResolverAgent — load stored auth for the URL, or save new auth supplied at call-time.

Resolution order
----------------
1.  If `auth_input` is provided AND non-trivial → that wins. Saved to Postgres so the
    next call to the same URL can omit auth.
2.  Else  → load whatever is registered for (method, url) from Postgres / Redis.
3.  Else  → fall back to AuthSpec(type="none").

If `save_auth=True` was passed explicitly, the resolved auth is upserted even when
it came from the registry (used by the `register` CLI command).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..store import AuthSpec, CredentialStore, endpoint_key

if TYPE_CHECKING:
    from ..state import AgentState
    from ..config import AgentConfig


def _is_meaningful_auth(payload: dict | None) -> bool:
    if not payload:
        return False
    if (payload.get("type") or "none") != "none":
        return True
    return bool(payload.get("headers"))


def auth_resolver(state: "AgentState", cfg: "AgentConfig") -> dict:
    if state.get("error"):
        return {}

    store = CredentialStore(cfg)
    method = state["method"]
    url    = state["url"]
    eid    = endpoint_key(method, url)

    incoming = state.get("auth_input") or {}
    save_flag = bool(state.get("save_auth"))

    chosen_source = "none"
    chosen: AuthSpec
    default_headers: dict = {}
    default_query:   dict = {}

    if _is_meaningful_auth(incoming):
        chosen = AuthSpec.from_dict(incoming)
        chosen_source = "input"
    else:
        rec = store.load_endpoint(method, url)
        if rec:
            chosen = AuthSpec.from_dict(rec.get("auth") or {})
            default_headers = rec.get("default_headers") or {}
            default_query   = rec.get("default_query") or {}
            chosen_source = "stored"
        else:
            chosen = AuthSpec(type="none")

    if chosen_source == "input" or save_flag:
        try:
            store.save_endpoint(
                method=method, url=url, auth=chosen,
                name=state.get("name", ""),
                default_headers=state.get("headers") or default_headers,
                default_query=state.get("query") or default_query,
            )
        except Exception as e:
            return {
                "endpoint_id": eid,
                "resolved_auth": chosen.to_dict(),
                "stages": [{
                    "agent": "AuthResolverAgent",
                    "status": "ERROR",
                    "msg": f"persist failed: {e}",
                }],
            }

    # Merge defaults under per-call overrides.
    merged_headers = {**default_headers, **(state.get("headers") or {})}
    merged_query   = {**default_query,   **(state.get("query")   or {})}

    return {
        "endpoint_id": eid,
        "resolved_auth": chosen.to_dict(),
        "request_headers": merged_headers,
        "request_query":   merged_query,
        "stages": [{
            "agent": "AuthResolverAgent",
            "status": "OK",
            "msg": f"auth={chosen.type} source={chosen_source} saved={chosen_source == 'input' or save_flag}",
        }],
    }
