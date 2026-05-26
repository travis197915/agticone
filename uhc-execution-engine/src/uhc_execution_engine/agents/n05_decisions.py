"""n05 — evaluate every decision rule, one LLM call each.

Skipped entirely when a blocking precondition already terminated the run.
"""
from __future__ import annotations

import time

from ..config import get_config
from ..state import ExecutionState
from ._eval_common import _tool_context_for_rule, evaluate_one_rule


def evaluate_decisions(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    if state.get("status") in ("FAILED", "TERMINATED_BY_PRECONDITION"):
        stages.append({"node": "evaluate_decisions", "status": "SKIP",
                       "ms": int((time.time() - t0) * 1000)})
        return {"decision_results": [], "stages": stages}

    cfg = get_config()
    claim = state.get("claim") or {}
    tools_by_rule = state.get("tools_by_rule_key") or {}
    tools_by_shape = state.get("tools_by_shape") or {}
    tool_results = state.get("tool_results") or {}

    results: list[dict] = []
    for idx, rule in enumerate(state.get("decisions") or []):
        ctx, binding_ids = _tool_context_for_rule(
            rule, tools_by_rule, tools_by_shape, tool_results)
        verdict, meta = evaluate_one_rule(
            cfg, rule=rule, claim=claim, tool_context=ctx,
            stage="evaluate_decisions")
        results.append({
            "order_index": idx,
            "rule_key": rule["key"],
            "binding_id": rule.get("binding_id", ""),
            "source": "decision",
            "condition": rule.get("condition", ""),
            "action": rule.get("action", ""),
            "matched": bool(verdict.get("matched")),
            "confidence": float(verdict.get("confidence") or 0.0),
            "reasoning": str(verdict.get("reasoning") or ""),
            "decision_type": rule.get("decision_type", ""),
            "codes": list(rule.get("codes") or []),
            "tool_results_used": binding_ids,
            "llm_provider": meta.get("provider", ""),
            "llm_ms": int(meta.get("ms") or 0),
        })

    stages.append({"node": "evaluate_decisions", "status": "OK",
                   "ms": int((time.time() - t0) * 1000),
                   "msg": f"{len(results)} rules"})
    return {"decision_results": results, "stages": stages}
