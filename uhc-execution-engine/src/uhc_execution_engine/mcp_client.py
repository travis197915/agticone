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

from .tool_telemetry import classify_tool_error

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


def mcp_timeout_seconds() -> int:
    """Configured per-request MCP HTTP timeout (seconds)."""
    cfg = _active_config()
    if not cfg:
        try:
            return int(os.environ.get("MCP_SERVER_TIMEOUT_SECONDS", "30"))
        except ValueError:
            return 30
    return int(cfg.get("timeout") or 30)


def parallel_tool_invoke_timeout_seconds() -> float | None:
    """Upper bound for one parallel prefetch in ``run_tools``.

    Defaults to ``MCP timeout × max attempts + slack`` so hung MCP calls do
    not block the whole claim forever. Set ``RULE_ENGINE_TOOL_INVOKE_TIMEOUT_SECONDS=0``
    to disable (wait indefinitely).
    """
    raw = os.environ.get("RULE_ENGINE_TOOL_INVOKE_TIMEOUT_SECONDS", "").strip()
    if raw.lower() in {"0", "off", "none", "false"}:
        return None
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(mcp_timeout_seconds() * _MCP_MAX_ATTEMPTS + 10)


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


def _request_url(cfg: dict[str, Any], path: str) -> str:
    return f"{cfg['base_url']}{path if path.startswith('/') else '/' + path}"


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
    url = _request_url(cfg, path)
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers[cfg["auth_header"]] = cfg["api_key"]
    body = {cfg["claim_arg"]: claim_id}
    timeout_s = int(cfg["timeout"])

    t0 = time.time()
    last_exc: Exception | None = None
    attempts_used = 0
    for attempt in range(_MCP_MAX_ATTEMPTS):
        attempts_used = attempt + 1
        logger.info(
            "mcp_invoke start tool=%s claim=%s method=%s path=%s timeout_s=%s attempt=%d/%d",
            tool_name,
            claim_id or "-",
            cfg["http_method"],
            path,
            timeout_s,
            attempts_used,
            _MCP_MAX_ATTEMPTS,
        )
        try:
            resp = requests.request(
                cfg["http_method"], url, json=body, headers=headers,
                timeout=timeout_s,
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
            duration_ms = int((time.time() - t0) * 1000)
            return {
                "ok": True, "tool": tool_name, "args": dict(args),
                "result": result, "error": "",
                "duration_ms": duration_ms,
                "timeout_s": timeout_s,
                "attempts": attempts_used,
                "error_kind": "",
            }
        except _RETRYABLE_ERRORS as exc:
            last_exc = exc
            elapsed_ms = int((time.time() - t0) * 1000)
            kind = classify_tool_error(exc)
            if isinstance(exc, requests.exceptions.Timeout):
                logger.warning(
                    "mcp_invoke TIMEOUT tool=%s claim=%s path=%s timeout_s=%s "
                    "elapsed_ms=%s attempt=%d/%d",
                    tool_name,
                    claim_id or "-",
                    path,
                    timeout_s,
                    elapsed_ms,
                    attempts_used,
                    _MCP_MAX_ATTEMPTS,
                )
            else:
                logger.warning(
                    "mcp_invoke %s tool=%s claim=%s path=%s elapsed_ms=%s "
                    "attempt=%d/%d: %s",
                    kind,
                    tool_name,
                    claim_id or "-",
                    path,
                    elapsed_ms,
                    attempts_used,
                    _MCP_MAX_ATTEMPTS,
                    exc,
                )
            if attempt + 1 < _MCP_MAX_ATTEMPTS:
                time.sleep(_MCP_BACKOFF_SECONDS * (attempt + 1))
                continue
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "mcp_invoke failed tool=%s claim=%s path=%s attempt=%d/%d: %s",
                tool_name,
                claim_id or "-",
                path,
                attempts_used,
                _MCP_MAX_ATTEMPTS,
                exc,
            )
            break

    duration_ms = int((time.time() - t0) * 1000)
    error_kind = classify_tool_error(last_exc)
    if error_kind == "timeout":
        logger.warning(
            "mcp_invoke TIMEOUT (final) tool=%s claim=%s path=%s timeout_s=%s "
            "elapsed_ms=%s attempts=%d",
            tool_name,
            claim_id or "-",
            path,
            timeout_s,
            duration_ms,
            attempts_used,
        )
    return {
        "ok": False, "tool": tool_name, "args": dict(args),
        "result": None, "error": f"mcp call failed: {last_exc}",
        "duration_ms": duration_ms,
        "timeout_s": timeout_s,
        "attempts": attempts_used,
        "error_kind": error_kind,
    }
