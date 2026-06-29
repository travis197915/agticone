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

import logging
import time

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
    # Skipped (routed-past / not-applicable) rules never contribute to the
    # verdict. An out-of-scope match is normally a clean exclusion — EXCEPT when
    # it carries a code (e.g. "Deny F24 ... out of scope" or an EOB reference),
    # which is a real finding and must be able to drive a DEFECT.
    matched = [r for r in rule_results
               if r["matched"] and not r.get("skipped")
               and (not r.get("is_out_of_scope") or r.get("codes"))]
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

    # Deterministic clean-claim guard. A claim is a defect when a matched rule
    # applies an adverse disposition (DENY/STOP/PEND/REFER...) OR references an
    # EOB code (explicit verdict policy: a matched rule that references an EOB
    # code is a defect). If every matched rule is routing / data-gathering /
    # pass (CONDITIONAL, SYSTEM, ALLOW) and references no EOB code, the verdict
    # is ALLOW — decided here, deterministically, with NO LLM call.
    _ADVERSE = {"DENY", "STOP", "PEND", "REFER", "REFERRAL", "PENDED"}

    def _is_adverse(r: dict) -> bool:
        return ((r.get("decision_type") or "").upper() in _ADVERSE
                or bool(r.get("eob_codes")))

    adverse = [r for r in matched if _is_adverse(r)]
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

    # ── Deterministic DEFECT verdict ─────────────────────────────────────────
    # At least one matched rule is adverse (STOP/DENY/PEND/REFER) or references
    # an EOB code, so the claim is a DEFECT. Resolve the disposition by
    # precedence with NO LLM call — guaranteed defect + one fewer slow call.
    def _verdict_type(r: dict) -> str:
        dt = (r.get("decision_type") or "").upper()
        if dt in _PRECEDENCE and dt in _ADVERSE:
            return dt
        # Qualifies only via an EOB-code reference (or a non-precedence adverse
        # label) — treat as a DENY-level defect disposition.
        return "DENY"

    ranked = sorted(adverse, key=lambda r: _PRECEDENCE.index(_verdict_type(r)))
    winner = ranked[0]
    final_type = _verdict_type(winner)

    codes: list[str] = []
    for r in adverse:
        for c in (r.get("codes") or []):
            if c not in codes:
                codes.append(c)
    eob_referenced = sorted({c for r in adverse for c in (r.get("eob_codes") or [])})

    narrative = (
        f"DEFECT: {len(adverse)} matched rule(s) applied an adverse disposition "
        f"or referenced an EOB code. Highest-precedence disposition is "
        f"{final_type} (rule {winner.get('rule_key')}: {winner.get('reasoning') or ''})."
    )
    if eob_referenced:
        narrative += f" EOB code(s) referenced: {', '.join(eob_referenced)}."

    logger.info(
        "aggregate_decision claim=%s matched=%d adverse=%d -> deterministic %s "
        "(no LLM); eob=%s",
        state.get("claim_id") or "-", len(matched), len(adverse), final_type,
        eob_referenced or "-",
    )
    stages.append({"node": "aggregate_decision", "status": "OK",
                   "ms": int((time.time() - t0) * 1000),
                   "msg": f"deterministic DEFECT ({final_type})"})
    return {
        "final_decision_type": final_type,
        "applied_codes": codes,
        "narrative": narrative,
        "stages": stages,
    }
