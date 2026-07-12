"""Inner LangGraph — 6 sequential nodes (per-Shape evaluation)."""
from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from .agents import (aggregate_decision, execute_shapes, executive_summary,
                      load_bindings, persist_and_respond, run_tools,
                      validate_input)
from .state import ExecutionState


@lru_cache(maxsize=1)
def build_graph():
    g = StateGraph(ExecutionState)
    g.add_node("validate_input", validate_input)
    g.add_node("load_bindings", load_bindings)
    g.add_node("run_tools", run_tools)
    g.add_node("execute_shapes", execute_shapes)
    g.add_node("aggregate_decision", aggregate_decision)
    g.add_node("persist_and_respond", persist_and_respond)
    # Add-on: condense the persisted run into a human-auditor executive summary.
    g.add_node("executive_summary", executive_summary)

    g.add_edge(START, "validate_input")
    g.add_edge("validate_input", "load_bindings")
    g.add_edge("load_bindings", "run_tools")
    g.add_edge("run_tools", "execute_shapes")
    g.add_edge("execute_shapes", "aggregate_decision")
    g.add_edge("aggregate_decision", "persist_and_respond")
    g.add_edge("persist_and_respond", "executive_summary")
    g.add_edge("executive_summary", END)

    return g.compile()
