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

# Coverage-validation tools. Per the auditor spec, a *failed* coverage tool is a
# DEFECT for CoverageBenefit ("Coverage validation failed due to ... failed tool
# execution"). Any other tool a step relied on, when it fails, means the auditor
# cannot conclude → INCONCLUSIVE (never a silent ALLOW).
_COVERAGE_TOOLS = {
    "cbd_coverage", "check_medicare_coverage",
    "check_coverage_commercial", "check_coverage_medicaid",
}


def _failed_tools_relied_on(state: ExecutionState) -> tuple[set[str], set[str]]:
    """Return (failed_coverage_tools, failed_other_tools) for tools that a
    non-skipped, evaluated rule actually relied on. Tools attached to skipped
    (routed-past) steps are ignored — a human auditor doesn't fault a step the
    SOP told them to skip."""
    tool_results = state.get("tool_results") or {}
    failed_bindings = {
        str(bid) for bid, rec in tool_results.items()
        if isinstance(rec, dict) and not rec.get("ok", True)
    }
    if not failed_bindings:
        return set(), set()
    relied: set[str] = set()
    for r in (state.get("rule_results") or []):
        if r.get("skipped"):
            continue
        for bid in (r.get("tool_results_used") or []):
            if str(bid) in failed_bindings:
                relied.add(str(bid))
    names = {
        (tool_results.get(bid) or {}).get("tool_name", "")
        for bid in relied
    }
    names.discard("")
    cov = {n for n in names if n in _COVERAGE_TOOLS}
    return cov, names - cov


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
             if r.get("matched") and not r.get("skipped")
             and not r.get("is_out_of_scope")
             and r.get("decision_type") in {"DENY", "STOP"}),
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
    # Skipped (routed-past / not-applicable) and out-of-scope rules never
    # contribute to the verdict: a step the SOP told us to skip, or one whose
    # match means "out of scope / stop", is a clean exclusion, not a defect.
    matched = [r for r in rule_results
               if r["matched"] and not r.get("skipped")
               and not r.get("is_out_of_scope")]
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

    # Deterministic clean-claim guard. A claim is a defect ONLY when a matched
    # rule applies an adverse disposition (DENY/STOP/PEND/REFER...). If every
    # matched rule is routing / data-gathering / pass (CONDITIONAL, SYSTEM,
    # ALLOW), the verdict is ALLOW — decided here, deterministically, with NO
    # LLM call. This prevents the aggregator from ever hallucinating a defect
    # onto a structurally clean claim.
    _ADVERSE = {"DENY", "STOP", "PEND", "REFER", "REFERRAL", "PENDED"}
    adverse = [r for r in matched
               if (r.get("decision_type") or "").upper() in _ADVERSE]
    if not adverse:
        # Auditor guard: before signing off CLEAN, make sure the tools the
        # evaluated steps relied on actually returned. A failed *coverage* tool
        # is a DEFECT (per spec → REFER for manual coverage review); any other
        # failed tool the auditor needed makes the claim INCONCLUSIVE. Only a
        # claim whose needed tools all succeeded is a true ALLOW.
        cov_failed, other_failed = _failed_tools_relied_on(state)
        if cov_failed:
            narrative = (
                "Coverage validation could not be completed — coverage tool(s) "
                f"failed: {', '.join(sorted(cov_failed))}. Per audit policy a "
                "failed coverage check is a DEFECT; routing to manual review."
            )
            logger.info("aggregate_decision claim=%s coverage tool failure -> REFER (%s)",
                        state.get("claim_id") or "-", sorted(cov_failed))
            stages.append({"node": "aggregate_decision", "status": "OK",
                           "ms": int((time.time() - t0) * 1000),
                           "msg": "coverage tool failed; DEFECT (REFER)"})
            return {
                "final_decision_type": "REFER",
                "applied_codes": ["COVERAGE_VALIDATION_FAILED"],
                "narrative": narrative,
                "stages": stages,
            }
        if other_failed:
            narrative = (
                "Audit inconclusive — required tool(s) failed so the relevant "
                f"checks could not be evaluated: {', '.join(sorted(other_failed))}. "
                "No adverse disposition was found, but the claim cannot be "
                "cleared without this evidence."
            )
            logger.info("aggregate_decision claim=%s tool failure -> INCONCLUSIVE (%s)",
                        state.get("claim_id") or "-", sorted(other_failed))
            stages.append({"node": "aggregate_decision", "status": "OK",
                           "ms": int((time.time() - t0) * 1000),
                           "msg": "required tool failed; INCONCLUSIVE"})
            return {
                "final_decision_type": "INCONCLUSIVE",
                "applied_codes": [],
                "narrative": narrative,
                "stages": stages,
            }
        n_cond = sum(1 for r in matched
                     if (r.get("decision_type") or "").upper() not in _ADVERSE)
        logger.info(
            "aggregate_decision claim=%s matched=%d adverse=0 -> deterministic "
            "ALLOW (no LLM); %d routing/clean rules matched",
            state.get("claim_id") or "-", len(matched), n_cond,
        )
        stages.append({"node": "aggregate_decision", "status": "OK",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": "no adverse disposition; deterministic ALLOW"})
        return {
            "final_decision_type": "ALLOW",
            "applied_codes": [],
            "narrative": (
                f"No adverse disposition applied: {len(matched)} rule(s) "
                f"matched, all routing/clean (no DENY/REFER/PEND/STOP). "
                f"Verdict ALLOW."
            ),
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
