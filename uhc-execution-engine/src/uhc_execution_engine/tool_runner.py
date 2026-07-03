"""Invoke a `StructuredTool` from the `agent_tools` registry.

Mirrors the invoke→run→func fallback chain used inside
`agent_tools.graphs.single_tool_graph._invoke_tool_direct` so behavior is
consistent regardless of which entrypoint actually calls the tool.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from .tool_telemetry import classify_tool_error, log_tool_call

logger = logging.getLogger(__name__)


def _inprocess_timeout_seconds() -> float:
    try:
        return float(os.environ.get("AGENT_TOOLS_HTTP_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def invoke_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    phase: str = "",
    binding_id: str = "",
    claim_id: str = "",
) -> dict[str, Any]:
    """Run a single tool by name. Never raises; failures go in the result.

    Routing order:
    1. If ``MCP_SERVER_BASE_URL`` (env) or an active ``McpServerConfig`` (DB)
       exists and the tool has ``metadata['mcp_path']``, call ``base_url + path``.
    2. Otherwise fall back to the in-process ``agent_tools`` implementation.
    """
    from agent_tools.registry import get_tool

    from .mcp_client import mcp_invoke

    t0 = time.time()

    routed = mcp_invoke(tool_name, args)
    if routed is not None:
        log_tool_call(
            tool_name=tool_name,
            phase=phase,
            ok=bool(routed.get("ok")),
            duration_ms=int(routed.get("duration_ms") or 0),
            route="mcp",
            binding_id=binding_id,
            claim_id=claim_id,
            error=str(routed.get("error") or ""),
            args=routed.get("args") or args,
            timeout_s=routed.get("timeout_s"),
            error_kind=str(routed.get("error_kind") or ""),
            attempts=int(routed.get("attempts") or 1),
        )
        return routed

    timeout_s = _inprocess_timeout_seconds()
    tool = get_tool(tool_name)
    if tool is None:
        out = {
            "ok": False, "tool": tool_name, "args": dict(args),
            "result": None,
            "error": f"tool {tool_name!r} not found in agent_tools.registry",
            "duration_ms": int((time.time() - t0) * 1000),
        }
        log_tool_call(
            tool_name=tool_name,
            phase=phase,
            ok=False,
            duration_ms=out["duration_ms"],
            route="missing",
            binding_id=binding_id,
            claim_id=claim_id,
            error=out["error"],
            args=args,
        )
        return out

    try:
        if hasattr(tool, "invoke"):
            result = tool.invoke(args)
        elif hasattr(tool, "run"):
            result = tool.run(args)
        elif hasattr(tool, "func") and callable(tool.func):
            result = tool.func(**args)
        else:
            raise RuntimeError("tool has no invoke/run/func entrypoint")
    except Exception as exc:
        out = {
            "ok": False, "tool": tool_name, "args": dict(args),
            "result": None, "error": str(exc),
            "duration_ms": int((time.time() - t0) * 1000),
        }
        log_tool_call(
            tool_name=tool_name,
            phase=phase,
            ok=False,
            duration_ms=out["duration_ms"],
            route="inprocess",
            binding_id=binding_id,
            claim_id=claim_id,
            error=out["error"],
            args=args,
            timeout_s=timeout_s,
            error_kind=classify_tool_error(exc),
        )
        return out

    out = {
        "ok": True, "tool": tool_name, "args": dict(args),
        "result": result, "error": "",
        "duration_ms": int((time.time() - t0) * 1000),
    }
    log_tool_call(
        tool_name=tool_name,
        phase=phase,
        ok=True,
        duration_ms=out["duration_ms"],
        route="inprocess",
        binding_id=binding_id,
        claim_id=claim_id,
        args=args,
        timeout_s=timeout_s,
    )
    return out
