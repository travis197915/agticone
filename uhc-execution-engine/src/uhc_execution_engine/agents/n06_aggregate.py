"""n06 — aggregate_decision: one LLM call to fuse matched rules into a verdict.

Precedence: DENY > STOP > PEND > REFER > BYPASS > WAIVE > ALLOW. The LLM
gets the list of matched rules (from any Shape) and must produce a single
final outcome plus a deduplicated code list and a narrative explanation.
Conflicts (e.g. one ALLOW + one DENY both matched) are mentioned in the
narrative.

If the per-Shape evaluator already terminated the claim early
(``TERMINATED_EARLY``), this node produces a synthetic 'halted at shape X'
summary from the offending rule without burning an LLM call.
"""
from __future__ import annotations

import json
import logging
import time

from ..config import get_config
from ..llm import llm_call
from ..state import ExecutionState

logger = logging.getLogger(__name__)


_PRECEDENCE = ["DENY", "STOP", "PEND", "REFER", "BYPASS", "WAIVE",
               "CONDITIONAL", "SYSTEM", "ALLOW"]


def _fallback_aggregate(matched: list[dict]) -> dict:
    """Deterministic fallback used when the LLM aggregator fails."""
    if not matched:
        return {"final_decision_type": "ALLOW",
                "applied_codes": [],
                "narrative": "No decision rules matched; defaulting to ALLOW."}
    by_priority = sorted(
        matched,
        key=lambda r: _PRECEDENCE.index(r.get("decision_type", "ALLOW"))
        if r.get("decision_type") in _PRECEDENCE else len(_PRECEDENCE),
    )
    winner = by_priority[0]
    codes: list[str] = []
    for r in matched:
        for c in r.get("codes") or []:
            if c not in codes:
                codes.append(c)
    return {
        "final_decision_type": winner.get("decision_type") or "ALLOW",
        "applied_codes": codes,
        "narrative": f"Deterministic fallback: highest-precedence matched rule "
                     f"is {winner.get('rule_key')} ({winner.get('decision_type')}).",
    }


def aggregate_decision(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    if state.get("status") == "FAILED":
        return {}

    if state.get("status") == "TERMINATED_EARLY":
        # Halted mid-workflow when a Shape's rule matched with DENY/STOP.
        # Find the offending rule in rule_results (last matched DENY/STOP).
        results = list(state.get("rule_results") or [])
        halted = next(
            (r for r in reversed(results)
             if r.get("matched") and r.get("decision_type") in {"DENY", "STOP"}),
            None,
        )
        shape_id = state.get("terminated_at_shape_id") or ""
        if halted:
            shape_label = halted.get("shape_label") or shape_id or "an early shape"
            narrative = (f"Halted at shape {shape_label} by rule "
                         f"{halted['rule_key']}: {halted['reasoning']}")
            codes = list(halted.get("codes") or [])
            verdict = halted.get("decision_type") or "DENY"
        else:
            narrative = "Halted early; no DENY/STOP rule found in trace."
            codes, verdict = [], "DENY"
        stages.append({"node": "aggregate_decision", "status": "OK",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": f"early-halt summary (shape={shape_id})"})
        return {
            "final_decision_type": verdict,
            "applied_codes": codes,
            "narrative": narrative,
            "stages": stages,
        }

    rule_results = state.get("rule_results") or []
    matched = [r for r in rule_results if r["matched"]]
    if not matched:
        # No matches → no aggregator LLM call. Distinguish between
        # "evaluator ran rules but nothing matched" (legitimate ALLOW)
        # and "evaluator iterated zero rules" (silent misconfig).
        if not rule_results:
            logger.warning(
                "aggregate_decision claim=%s rule_results is empty; defaulting "
                "to ALLOW without any LLM call. This usually means execute_shapes "
                "had no shapes to iterate — check the load_bindings line above.",
                state.get("claim_id") or "-",
            )
        else:
            logger.info(
                "aggregate_decision claim=%s evaluated=%d matched=0 -> default ALLOW (no LLM)",
                state.get("claim_id") or "-", len(rule_results),
            )
        stages.append({"node": "aggregate_decision", "status": "OK",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": "no matches; default ALLOW"})
        return {
            "final_decision_type": "ALLOW",
            "applied_codes": [],
            "narrative": "No decision rules matched; defaulting to ALLOW.",
            "stages": stages,
        }

    cfg = get_config()
    compact = [{
        "rule_key": r["rule_key"],
        "decision_type": r["decision_type"],
        "codes": r["codes"],
        "reasoning": r["reasoning"],
        "confidence": r["confidence"],
    } for r in matched]

    prompt = f"""You are aggregating multiple matched SOP decision rules into one
final adjudication for a claim.

PRECEDENCE (most severe → least): {' > '.join(_PRECEDENCE)}

MATCHED DECISIONS
-----------------
{json.dumps(compact, indent=2)}

CLAIM (for context only)
------------------------
{json.dumps(state.get('claim') or {}, default=str, indent=2)}

Return JSON with exactly:
  final_decision_type   one of {_PRECEDENCE}
  applied_codes         deduplicated array of code strings drawn from the matched rules
  narrative             2-4 sentences explaining the outcome; explicitly call out any
                        conflicts between matched rules and how precedence resolved them
"""
    out, _meta = llm_call(
        cfg, prompt,
        agent_name="aggregate_decision",
        stage="aggregate_decision",
        fallback=_fallback_aggregate(matched),
        provider="anthropic",
        expected_type=dict,
        required_keys=["final_decision_type", "applied_codes", "narrative"],
    )

    stages.append({"node": "aggregate_decision", "status": "OK",
                   "ms": int((time.time() - t0) * 1000)})
    return {
        "final_decision_type": str(out.get("final_decision_type") or "ALLOW"),
        "applied_codes": list(out.get("applied_codes") or []),
        "narrative": str(out.get("narrative") or ""),
        "stages": stages,
    }
