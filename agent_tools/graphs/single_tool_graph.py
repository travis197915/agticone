"""
Minimal LangGraph runtime: a one-node :class:`StateGraph` that invokes a
single :class:`langchain_core.tools.StructuredTool` from the registry.

Why bother going through LangGraph instead of calling the tool directly?

* Shapes the invoke surface like the supervisor graph we'll eventually
  add — when that lands, we'll swap in a router node and reuse this
  ToolNode wrapper unchanged.
* Lets us add cross-cutting concerns (tracing, retries, structured
  logging) in one place per tool kind, not per tool.

The view layer talks to :func:`run_tool`; the graph itself is lazy-built
and cached per process.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from .. import registry

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _have_langgraph() -> bool:
    try:
        import langgraph  # noqa: F401
        from langgraph.graph import StateGraph  # noqa: F401
        return True
    except Exception:
        return False


def _invoke_tool_direct(tool, args: dict[str, Any]) -> Any:
    """Invoke the StructuredTool's underlying function with kwargs."""
    if hasattr(tool, "invoke"):
        try:
            return tool.invoke(args)
        except Exception as exc:
            logger.exception("tool.invoke(%s) failed: %s", tool.name, exc)
            raise
    if hasattr(tool, "run"):
        return tool.run(args)
    func = getattr(tool, "func", None) or getattr(tool, "_run", None)
    if func is None:
        raise RuntimeError(f"tool '{tool.name}' has no callable surface")
    return func(**args)


def run_tool(name: str, args: dict[str, Any]) -> Any:
    """Run the named tool through a fresh single-tool LangGraph.

    When LangGraph is unavailable (older deployment), we fall back to a
    direct StructuredTool call so the registry remains usable.
    """
    tool = registry.get_tool(name)
    if tool is None:
        raise LookupError(f"unknown tool '{name}'")

    if not _have_langgraph():
        return _invoke_tool_direct(tool, args)

    from langgraph.graph import END, START, StateGraph

    def _node(state: dict[str, Any]) -> dict[str, Any]:
        result = _invoke_tool_direct(tool, state.get("args") or {})
        return {**state, "result": result}

    graph = StateGraph(dict)
    graph.add_node("tool", _node)
    graph.add_edge(START, "tool")
    graph.add_edge("tool", END)
    compiled = graph.compile()
    final = compiled.invoke({"args": dict(args), "name": name})
    return (final or {}).get("result")
