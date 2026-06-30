"""Invoke a `StructuredTool` from the `agent_tools` registry.

Mirrors the invoke→run→func fallback chain used inside
`agent_tools.graphs.single_tool_graph._invoke_tool_direct` so behavior is
consistent regardless of which entrypoint actually calls the tool.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def invoke_tool(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
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
        return routed

    tool = get_tool(tool_name)
    if tool is None:
        return {
            "ok": False, "tool": tool_name, "args": dict(args),
            "result": None,
            "error": f"tool {tool_name!r} not found in agent_tools.registry",
            "duration_ms": int((time.time() - t0) * 1000),
        }

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
        return {
            "ok": False, "tool": tool_name, "args": dict(args),
            "result": None, "error": str(exc),
            "duration_ms": int((time.time() - t0) * 1000),
        }

    return {
        "ok": True, "tool": tool_name, "args": dict(args),
        "result": result, "error": "",
        "duration_ms": int((time.time() - t0) * 1000),
    }
