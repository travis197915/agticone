"""n04 — evaluate every precondition rule, one LLM call each.

A failure on any ``is_blocking`` precondition short-circuits the pipeline:
the rest of the run is skipped and the response is marked
``TERMINATED_BY_PRECONDITION``.
"""
from __future__ import annotations

import time

from ..config import get_config
from ..state import ExecutionState
from ._eval_common import _tool_context_for_rule, evaluate_one_rule


def evaluate_preconditions(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    if state.get("status") == "FAILED":
        return {}

    cfg = get_config()
    claim = state.get("claim") or {}
    tools_by_rule = state.get("tools_by_rule_key") or {}
    tools_by_shape = state.get("tools_by_shape") or {}
    tool_results = state.get("tool_results") or {}

    results: list[dict] = []
    terminate = False
    for idx, rule in enumerate(state.get("preconditions") or []):
        ctx, binding_ids = _tool_context_for_rule(
            rule, tools_by_rule, tools_by_shape, tool_results)
        verdict, meta = evaluate_one_rule(
            cfg, rule=rule, claim=claim, tool_context=ctx,
            stage="evaluate_preconditions")
        matched = bool(verdict.get("matched"))
        results.append({
            "order_index": idx,
            "rule_key": rule["key"],
            "binding_id": rule.get("binding_id", ""),
            "source": "precondition",
            "condition": rule.get("condition", ""),
            "action": rule.get("action", ""),
            "matched": matched,
            "confidence": float(verdict.get("confidence") or 0.0),
            "reasoning": str(verdict.get("reasoning") or ""),
            "decision_type": rule.get("decision_type", ""),
            "codes": list(rule.get("codes") or []),
            "tool_results_used": binding_ids,
            "llm_provider": meta.get("provider", ""),
            "llm_ms": int(meta.get("ms") or 0),
        })
        # Blocking precondition that fails → halt. We treat a precondition as
        # "failed" when the LLM says the condition matched AND the rule's
        # decision_type is DENY / STOP, OR when the rule is_exception==True
        # and matched. Simpler heuristic for v1: blocking + matched ⇒ stop
        # (preconditions in SOPs typically encode "must hold" gates).
        if rule.get("is_blocking") and matched and (
            rule.get("decision_type") in {"DENY", "STOP"}
            or rule.get("is_exception")
        ):
            terminate = True

    stages.append({
        "node": "evaluate_preconditions", "status": "OK",
        "ms": int((time.time() - t0) * 1000),
        "msg": f"{len(results)} rules; terminate={terminate}",
    })
    out: dict = {"precondition_results": results, "stages": stages}
    if terminate:
        out["terminate"] = True
        out["status"] = "TERMINATED_BY_PRECONDITION"
    return out
