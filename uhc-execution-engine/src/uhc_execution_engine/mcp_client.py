"""Route a tool call to an external claims MCP/REST server.

Storage model:
* The **base endpoint** + auth live once in ``agent_tools.McpServerConfig``
  (one active row).
* Each tool stores **only its path** in ``Tool.metadata['mcp_path']``
  (e.g. ``/tools/facets_get_summary``).

At call time we join ``base_url + path`` and POST the claim identifier. The
server returns a ``ToolCallResult`` envelope; we unwrap ``response.body`` (or
``response``) as the tool result so downstream rule evaluation sees the same
shape it would from the in-process tool.

If there is no active config or the tool has no ``mcp_path`` we return ``None``
so the caller falls back to the in-process ``agent_tools`` implementation.
This keeps the whole feature additive / backward compatible.
"""
from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Retry policy for transient network/TLS failures talking to the MCP host.
_MCP_MAX_ATTEMPTS = 3
_MCP_BACKOFF_SECONDS = 0.75
_RETRYABLE_ERRORS = (
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


@lru_cache(maxsize=1)
def _active_config() -> dict[str, Any] | None:
    """Most-recently-updated active McpServerConfig as a plain dict (cached)."""
    try:
        from agent_tools.models import McpServerConfig
    except Exception:  # pragma: no cover - app not ready
        return None
    cfg = McpServerConfig.objects.filter(is_active=True).order_by("-updated_at").first()
    if not cfg:
        return None
    return {
        "base_url": cfg.base_url.rstrip("/"),
        "auth_header": cfg.auth_header,
        "api_key": cfg.api_key,
        "http_method": (cfg.http_method or "POST").upper(),
        "claim_arg": cfg.claim_arg or "claim_number",
        "timeout": cfg.timeout_seconds or 30,
    }


def _tool_path(tool_name: str) -> str:
    try:
        from agent_tools.models import Tool
    except Exception:  # pragma: no cover
        return ""
    tool = Tool.objects.filter(name=tool_name).only("metadata").first()
    if not tool:
        return ""
    return (tool.metadata or {}).get("mcp_path") or ""


def reset_cache() -> None:
    _active_config.cache_clear()


def _claim_id_from_args(args: dict[str, Any]) -> str:
    for k in ("claim_number", "claim_id", "subscriber_id", "member_id"):
        v = args.get(k)
        if v:
            return str(v)
    return ""


def mcp_invoke(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Call the external server for ``tool_name``. Returns a tool_runner-shaped
    dict, or ``None`` when routing is not configured for this tool."""
    cfg = _active_config()
    if not cfg:
        return None
    path = _tool_path(tool_name)
    if not path:
        return None

    claim_id = _claim_id_from_args(args)
    url = f"{cfg['base_url']}{path if path.startswith('/') else '/' + path}"
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers[cfg["auth_header"]] = cfg["api_key"]
    body = {cfg["claim_arg"]: claim_id}

    t0 = time.time()
    # Transient TLS/connection blips on the MCP host (e.g. an incomplete cert
    # chain served mid-deploy, or a dropped connection) must not fail an entire
    # claim at the fetch step. Retry a few times with a short backoff; only
    # network-level errors are retried, not HTTP 4xx/5xx responses.
    last_exc: Exception | None = None
    for attempt in range(_MCP_MAX_ATTEMPTS):
        try:
            resp = requests.request(
                cfg["http_method"], url, json=body, headers=headers,
                timeout=cfg["timeout"],
            )
            resp.raise_for_status()
            payload = resp.json()
            # Unwrap the ToolCallResult envelope → the data the rules care about.
            result: Any = payload
            if isinstance(payload, dict):
                inner = payload.get("response", payload)
                if isinstance(inner, dict) and "body" in inner:
                    result = inner["body"]
                else:
                    result = inner
            return {
                "ok": True, "tool": tool_name, "args": dict(args),
                "result": result, "error": "",
                "duration_ms": int((time.time() - t0) * 1000),
            }
        except _RETRYABLE_ERRORS as exc:
            last_exc = exc
            if attempt + 1 < _MCP_MAX_ATTEMPTS:
                logger.warning(
                    "mcp_invoke %s transient error (attempt %d/%d): %s",
                    tool_name, attempt + 1, _MCP_MAX_ATTEMPTS, exc,
                )
                time.sleep(_MCP_BACKOFF_SECONDS * (attempt + 1))
                continue
        except Exception as exc:  # non-retryable (HTTP error, bad JSON, …)
            last_exc = exc
            break

    return {
        "ok": False, "tool": tool_name, "args": dict(args),
        "result": None, "error": f"mcp call failed: {last_exc}",
        "duration_ms": int((time.time() - t0) * 1000),
    }
