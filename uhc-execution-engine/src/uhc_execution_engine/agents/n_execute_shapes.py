"""execute_shapes — iterate the workflow's Shapes in canvas order.

For each Shape:
  • Evaluate every rule attached to that Shape (preconditions + decisions
    together) via the per-rule LLM helper in ``_eval_common``.
  • After all rules on the Shape are evaluated, if any matched rule's
    decision_type is DENY or STOP → halt the claim immediately. Otherwise
    move on to the next Shape.

Preconditions and decisions are evaluated by the same loop: a precondition
is just a rule with ``rule_source == "PRECONDITION"`` that happens to live
on a Shape. Early halts emit ``status="TERMINATED_EARLY"`` and the
offending shape id is captured in ``state["terminated_at_shape_id"]``.
"""
from __future__ import annotations

import logging
import time

from ..config import get_config
from ..llm import publish_event
from ..state import ExecutionState
from ._eval_common import _tool_context_for_rule, evaluate_one_rule

logger = logging.getLogger(__name__)

_HALT_DECISION_TYPES = {"DENY", "STOP"}


def execute_shapes(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    if state.get("status") == "FAILED":
        return {}

    cfg = get_config()
    claim = state.get("claim") or {}
    tools_by_rule = state.get("tools_by_rule_key") or {}
    tools_by_shape = state.get("tools_by_shape") or {}
    tool_results = state.get("tool_results") or {}
    shapes = state.get("shapes") or []

    rule_results: list[dict] = []
    order_index = 0
    terminated_at_shape_id = ""
    shapes_evaluated = 0

    claim_id = state.get("claim_id", "")

    if not shapes:
        # Hard-to-spot failure mode: load_bindings accepted the workflow
        # (flat pre/dec non-empty) but no shape carries any rules, so the
        # per-rule LLM is never reached and the claim defaults to ALLOW
        # in aggregate_decision. See EXECUTION_ENGINE.md §9 for details.
        logger.warning(
            "execute_shapes claim=%s zero shapes to iterate; no rule_evaluated "
            "events will fire and Anthropic will not be called for this claim",
            claim_id or "-",
        )

    for shape in shapes:
        shapes_evaluated += 1
        shape_id = shape.get("shape_id", "")
        shape_label = shape.get("shape_label", "")
        rules = shape.get("rules") or []
        shape_t0 = time.time()
        matched_count = 0
        halt_after_shape = False

        logger.info(
            "execute_shapes claim=%s shape=%s (%s) rules=%d",
            claim_id or "-", shape_id, shape_label or "-", len(rules),
        )

        # SSE side channel: announce the Shape so the SPA can open a
        # group and render a progress bar. No-op when no batch is in scope.
        publish_event("shape_start", {
            "claim_id": claim_id,
            "shape_id": shape_id,
            "shape_label": shape_label,
            "rules_total": len(rules),
        })

        for rule in rules:
            ctx, binding_ids = _tool_context_for_rule(
                rule, tools_by_rule, tools_by_shape, tool_results)
            verdict, meta = evaluate_one_rule(
                cfg, rule=rule, claim=claim, tool_context=ctx,
                stage="execute_shapes")
            matched = bool(verdict.get("matched"))
            rule_results.append({
                "order_index": order_index,
                "shape_id": shape_id,
                "shape_label": shape_label,
                "rule_key": rule["key"],
                "binding_id": rule.get("binding_id", ""),
                "source": rule.get("source", "decision"),
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

            # SSE side channel: one event per rule, retries collapsed.
            publish_event("rule_evaluated", {
                "claim_id": claim_id,
                "shape_id": shape_id,
                "shape_label": shape_label,
                "rule_key": rule["key"],
                "rule_source": rule.get("source", "decision"),
                "matched": matched,
                "decision_type": rule.get("decision_type", ""),
                "confidence": float(verdict.get("confidence") or 0.0),
                "reasoning": str(verdict.get("reasoning") or ""),
                "codes": list(rule.get("codes") or []),
                "llm_provider": meta.get("provider", ""),
                "llm_model": meta.get("model", ""),
                "llm_ms": int(meta.get("ms") or 0),
                "llm_attempts": int(meta.get("attempts") or 1),
            })

            order_index += 1
            if matched:
                matched_count += 1
                if rule.get("decision_type") in _HALT_DECISION_TYPES:
                    halt_after_shape = True

        stages.append({
            "node": "execute_shapes",
            "status": "OK",
            "ms": int((time.time() - shape_t0) * 1000),
            "msg": (f"shape={shape_label or shape_id}: "
                    f"{len(rules)} rules, {matched_count} matched"
                    + (" → TERMINATED_EARLY" if halt_after_shape else "")),
        })

        if halt_after_shape:
            terminated_at_shape_id = shape_id
            break

    stages.append({
        "node": "execute_shapes:summary", "status": "OK",
        "ms": int((time.time() - t0) * 1000),
        "msg": f"{len(rule_results)} rules across {shapes_evaluated} shapes",
    })
    out: dict = {"rule_results": rule_results, "stages": stages}
    if terminated_at_shape_id:
        out["status"] = "TERMINATED_EARLY"
        out["terminated_at_shape_id"] = terminated_at_shape_id
    return out
