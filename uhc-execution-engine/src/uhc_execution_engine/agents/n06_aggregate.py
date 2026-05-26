"""n06 — aggregate_decision: one LLM call to fuse matched decisions.

Precedence: DENY > STOP > PEND > REFER > BYPASS > WAIVE > ALLOW. The LLM
gets the matched-decision list and must produce a single final outcome plus
a deduplicated code list and a narrative explanation. Conflicts (e.g. one
ALLOW + one DENY both matched) are mentioned in the narrative.

If we already terminated on a precondition, this node produces a synthetic
'precondition failed' summary without burning an LLM call.
"""
from __future__ import annotations

import json
import time

from ..config import get_config
from ..llm import llm_call
from ..state import ExecutionState


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

    if state.get("status") == "TERMINATED_BY_PRECONDITION":
        blocking = next(
            (r for r in (state.get("precondition_results") or [])
             if r["matched"]
             and r["decision_type"] in {"DENY", "STOP"}),
            None,
        )
        narrative = ("Blocked by precondition "
                     f"{blocking['rule_key']}: {blocking['reasoning']}"
                     if blocking
                     else "Blocked by a precondition.")
        codes = list(blocking.get("codes") or []) if blocking else []
        stages.append({"node": "aggregate_decision", "status": "OK",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": "precondition-block summary"})
        return {
            "final_decision_type": (blocking.get("decision_type") if blocking else "DENY"),
            "applied_codes": codes,
            "narrative": narrative,
            "stages": stages,
        }

    matched = [r for r in (state.get("decision_results") or []) if r["matched"]]
    if not matched:
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
