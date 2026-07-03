"""Route a tool call to an external claims MCP/REST server.

Storage model (priority order):
1. **Environment** — ``MCP_SERVER_BASE_URL`` + optional auth/method vars (prod).
2. **Database** — one active ``agent_tools.McpServerConfig`` row (builder UI).

Each tool stores **only its path** in ``Tool.metadata['mcp_path']``
(e.g. ``/tools/facets_get_summary``). At call time we join ``base_url + path``.

If there is no active config or the tool has no ``mcp_path`` we return ``None``
so the caller falls back to the in-process ``agent_tools`` implementation.
"""
from __future__ import annotations

import logging
import os
import time
from functools import lru_cache
from typing import Any, Literal

import requests

logger = logging.getLogger(__name__)

ConfigSource = Literal["env", "db", "none"]

# Retry policy for transient network/TLS failures talking to the MCP host.
_MCP_MAX_ATTEMPTS = 3
_MCP_BACKOFF_SECONDS = 0.75
_RETRYABLE_ERRORS = (
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


def _config_dict(
    *,
    base_url: str,
    auth_header: str,
    api_key: str,
    http_method: str,
    claim_arg: str,
    timeout: int,
) -> dict[str, Any]:
    return {
        "base_url": base_url.rstrip("/"),
        "auth_header": auth_header or "x-api-key",
        "api_key": api_key or "",
        "http_method": (http_method or "POST").upper(),
        "claim_arg": claim_arg or "claim_number",
        "timeout": timeout or 30,
    }


def _config_from_env() -> dict[str, Any] | None:
    """Load MCP server connection from env (12-factor / production default)."""
    base_url = os.environ.get("MCP_SERVER_BASE_URL", "").strip()
    if not base_url:
        return None
    try:
        timeout = int(os.environ.get("MCP_SERVER_TIMEOUT_SECONDS", "30"))
    except ValueError:
        timeout = 30
    return _config_dict(
        base_url=base_url,
        auth_header=os.environ.get("MCP_SERVER_AUTH_HEADER", "x-api-key"),
        api_key=os.environ.get("MCP_SERVER_API_KEY", ""),
        http_method=os.environ.get("MCP_SERVER_HTTP_METHOD", "POST"),
        claim_arg=os.environ.get("MCP_SERVER_CLAIM_ARG", "claim_number"),
        timeout=timeout,
    )


@lru_cache(maxsize=1)
def _active_config_from_db() -> dict[str, Any] | None:
    """Most-recently-updated active McpServerConfig (cached per process)."""
    try:
        from agent_tools.models import McpServerConfig
    except Exception:  # pragma: no cover - app not ready
        return None
    cfg = McpServerConfig.objects.filter(is_active=True).order_by("-updated_at").first()
    if not cfg:
        return None
    return _config_dict(
        base_url=cfg.base_url,
        auth_header=cfg.auth_header,
        api_key=cfg.api_key,
        http_method=cfg.http_method,
        claim_arg=cfg.claim_arg,
        timeout=cfg.timeout_seconds,
    )


def active_config_source() -> ConfigSource:
    """Where the runtime MCP connection settings come from."""
    if _config_from_env() is not None:
        return "env"
    if _active_config_from_db() is not None:
        return "db"
    return "none"


def _active_config() -> dict[str, Any] | None:
    """Env wins over DB so production secrets stay out of Postgres."""
    env_cfg = _config_from_env()
    if env_cfg is not None:
        return env_cfg
    return _active_config_from_db()


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
    """Clear the DB config cache (env is read fresh each call)."""
    _active_config_from_db.cache_clear()


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
    last_exc: Exception | None = None
    for attempt in range(_MCP_MAX_ATTEMPTS):
        try:
            resp = requests.request(
                cfg["http_method"], url, json=body, headers=headers,
                timeout=cfg["timeout"],
            )
            resp.raise_for_status()
            payload = resp.json()
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
        except Exception as exc:
            last_exc = exc
            break

    return {
        "ok": False, "tool": tool_name, "args": dict(args),
        "result": None, "error": f"mcp call failed: {last_exc}",
        "duration_ms": int((time.time() - t0) * 1000),
    }
