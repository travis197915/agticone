"""n03 — run_tools: execute every non-fetch tool binding for the workflow.

We've already fetched + parsed the claim in the outer layer, so we skip
``linx_claim_search`` and ``llm_parse_claim_with_ontology`` here to avoid
double work.
"""
from __future__ import annotations

import time
from typing import Any

from ..claim_fetcher import FETCH_TOOL, PARSE_TOOL
from ..config import get_config
from ..state import ExecutionState
from ..tool_runner import invoke_tool

_SKIP_TOOLS = {FETCH_TOOL, PARSE_TOOL}


def _merge_args(template: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
    """Start from the binding's args_template, then fill in any obvious
    claim-derived defaults the tool is likely to want.
    """
    args: dict[str, Any] = dict(template or {})
    # Common claim fields tools may want; non-destructive (template wins).
    for key in ("subscriber_id", "member_id", "claim_id",
                "diagnosis_code", "cpt_code", "place_of_service",
                "first_name", "last_name", "dob"):
        if key in claim and key not in args:
            args[key] = claim[key]
    return args


def run_tools(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    if state.get("status") == "FAILED":
        return {}

    claim = state.get("claim") or {}
    invocations = list(state.get("tool_invocations") or [])
    results_by_binding: dict[str, dict[str, Any]] = dict(state.get("tool_results") or {})

    # Lazy mode: defer EVALUATE-phase tool invocation to execute_shapes, which
    # only runs the tools for steps the router actually reaches. Pre-seeded
    # results (injected/cached) are preserved and reused as before.
    if get_config().lazy_tools:
        stages.append({"node": "run_tools", "status": "OK",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": "deferred to execute_shapes (lazy_tools)"})
        return {
            "tool_invocations": invocations,
            "tool_results": results_by_binding,
            "stages": stages,
        }

    # Collect every unique tool binding from both scoping maps
    seen: set[str] = set()
    bindings: list[dict[str, Any]] = []
    for tb_list in (state.get("tools_by_shape") or {}).values():
        for tb in tb_list:
            if tb["binding_id"] in seen:
                continue
            if tb["tool_name"] in _SKIP_TOOLS:
                continue
            # Reuse a pre-seeded result (e.g. injected/cached) instead of
            # re-invoking the tool live. Backward compatible: empty seed map
            # means every binding is invoked as before.
            if tb["binding_id"] in results_by_binding:
                continue
            seen.add(tb["binding_id"])
            bindings.append(tb)

    for tb in bindings:
        args = _merge_args(tb["args_template"], claim)
        out = invoke_tool(tb["tool_name"], args)
        record = {
            "binding_id": tb["binding_id"],
            "tool_name": tb["tool_name"],
            "phase": "EVALUATE",
            "args": out.get("args") or args,
            "ok": out["ok"],
            "result": out["result"],
            "error": out["error"],
            "duration_ms": out["duration_ms"],
        }
        invocations.append(record)
        results_by_binding[tb["binding_id"]] = record

    stages.append({"node": "run_tools", "status": "OK",
                   "ms": int((time.time() - t0) * 1000),
                   "msg": f"invoked {len(bindings)} tools"})
    return {
        "tool_invocations": invocations,
        "tool_results": results_by_binding,
        "stages": stages,
    }
