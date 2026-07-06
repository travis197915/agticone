"""n07 — persist_and_respond: write run + evaluation + tool rows; build response."""
from __future__ import annotations

import logging
import time
from typing import Any

from django.db import transaction
from django.utils import timezone

from ..config import get_config
from ..memory import apply_claim_level_policy, update_claim_memory
from ..state import ExecutionState
from ..xlsx_parser import EXCEL_BILLING_FIELDS

logger = logging.getLogger(__name__)


# USD per 1,000,000 tokens. Anthropic + OpenAI public list prices, current as
# of Sept 2026. When provider/model strings include a date suffix (e.g.
# ``claude-sonnet-4-5-20250929``), the lookup also tries the base name.
# Add new models here when introduced; an unmatched (provider, model) contributes
# 0 to the cost AND is recorded in ``cost_breakdown[key]['cost_usd']=None`` so
# it's visible in the explainability blob without inflating the total.
_RATES_USD_PER_MTOK: dict[tuple[str, str], dict[str, float]] = {
    ("anthropic", "claude-sonnet-4-5"):          {"in": 3.0, "out": 15.0},
    ("anthropic", "claude-sonnet-4-5-20250929"): {"in": 3.0, "out": 15.0},
    ("openai",    "gpt-4o"):                      {"in": 2.5, "out": 10.0},
}


def _rate_for(provider: str, model: str) -> dict[str, float] | None:
    key = (provider, model)
    if key in _RATES_USD_PER_MTOK:
        return _RATES_USD_PER_MTOK[key]
    # Try base name without date suffix (`-20250929`, etc.)
    base = model.split("-2025")[0]
    return _RATES_USD_PER_MTOK.get((provider, base))


def _compute_run_cost(run_id: str) -> dict[str, Any]:
    """Aggregate every ``LLMCallLog`` row tagged with ``run_id`` into the
    claim's denormalized cost snapshot.

    Returns ``{total_prompt_tokens, total_completion_tokens, total_cost_usd,
    cost_breakdown}`` where ``cost_breakdown`` is keyed by ``"<provider>/<model>"``
    and carries ``{calls, prompt_tokens, completion_tokens, cost_usd}``.

    Failed calls (``success=False``) are counted in ``calls`` but not in
    ``cost_usd`` — the API rejected them, so they cost nothing. Their token
    counts are zero anyway, so the totals come out right either way.
    """
    from django.db.models import Count, Q, Sum
    from sop_ingestion.models import LLMCallLog

    rows = (LLMCallLog.objects
            .filter(execution_run_id=run_id)
            .values("llm_provider", "llm_model")
            .annotate(
                calls=Count("id"),
                sum_in=Sum("prompt_tokens"),
                sum_out=Sum("completion_tokens"),
                # Successful-only token sums for cost calc; we still report all
                # token counts in breakdown above.
                succ_in=Sum("prompt_tokens", filter=Q(success=True)),
                succ_out=Sum("completion_tokens", filter=Q(success=True)),
            ))

    breakdown: dict[str, dict[str, Any]] = {}
    total_in = 0
    total_out = 0
    total_cost = 0.0
    for r in rows:
        prov = r["llm_provider"] or ""
        mdl = r["llm_model"] or ""
        tin = int(r["sum_in"] or 0)
        tout = int(r["sum_out"] or 0)
        sin = int(r["succ_in"] or 0)
        sout = int(r["succ_out"] or 0)
        total_in += tin
        total_out += tout
        rate = _rate_for(prov, mdl)
        cost: float | None = None
        if rate is not None:
            cost = round(sin / 1_000_000 * rate["in"] + sout / 1_000_000 * rate["out"], 4)
            total_cost += cost
        breakdown[f"{prov}/{mdl}"] = {
            "calls": int(r["calls"] or 0),
            "prompt_tokens": tin,
            "completion_tokens": tout,
            "cost_usd": cost,
        }
    return {
        "total_prompt_tokens": total_in,
        "total_completion_tokens": total_out,
        "total_cost_usd": round(total_cost, 4),
        "cost_breakdown": breakdown,
    }


def _persist(state: ExecutionState) -> None:
    from execution_app.models import (RuleExecutionRun, RuleEvaluation,
                                         ToolInvocationRecord)

    status = state.get("status") or "COMPLETED"
    if status == "RUNNING":
        status = "COMPLETED"

    # Parent + children are written in one atomic block so a child failure
    # rolls the parent finalize back too — otherwise the parent would show
    # COMPLETED with zero children while the real error is swallowed.
    with transaction.atomic():
        # ``n01_validate`` reserved a RUNNING row with this run_id so that
        # LLMCallLog rows could FK to it during evaluation. Finalize it now.
        # update_or_create handles the rare case where validate failed
        # before reserving (e.g. missing workflow_id).
        run, _ = RuleExecutionRun.objects.update_or_create(
            id=state["run_id"],
            defaults=dict(
                batch_id=state.get("batch_id") or None,
                workflow_id=state["workflow_id"],
                claim_id=state.get("claim_id") or "",
                claim_payload=state.get("claim") or {},
                raw_fetch=state.get("raw_fetch") or {},
                finished_at=timezone.now(),
                status=status,
                final_decision_type=state.get("final_decision_type") or "",
                applied_codes=state.get("applied_codes") or [],
                narrative=state.get("narrative") or "",
                error_message=state.get("error_message") or "",
                claim_lob=state.get("claim_lob") or {},
            ),
        )

        all_evals = list(state.get("rule_results") or [])
        RuleEvaluation.objects.bulk_create([
            RuleEvaluation(
                run=run,
                order_index=ev["order_index"],
                rule_binding_id=ev["binding_id"] or None,
                rule_key=ev["rule_key"],
                rule_source=ev["source"].upper(),
                condition=ev["condition"],
                action=ev["action"],
                matched=ev["matched"],
                skipped=bool(ev.get("skipped")),
                skip_reason=str(ev.get("skip_reason") or "")[:255],
                confidence=ev["confidence"],
                reasoning=ev["reasoning"],
                decision_type=ev["decision_type"],
                verdict=ev["decision_type"] if ev["matched"] else "",
                codes=ev["codes"],
                tool_results_used=ev["tool_results_used"],
                live_result=ev.get("live_result"),
                overridden=bool(ev.get("overridden", False)),
                injected_context=ev.get("injected_context"),
                llm_provider=ev["llm_provider"],
                llm_ms=ev["llm_ms"],
            )
            for ev in all_evals
        ])

        ToolInvocationRecord.objects.bulk_create([
            ToolInvocationRecord(
                run=run,
                tool_binding_id=inv.get("binding_id") or None,
                tool_name=inv["tool_name"],
                phase=inv.get("phase", "EVALUATE"),
                args=inv.get("args") or {},
                ok=inv["ok"],
                result=inv.get("result") if isinstance(inv.get("result"), (dict, list)) else {"value": inv.get("result")},
                error=inv.get("error", ""),
                duration_ms=inv.get("duration_ms", 0),
                reused_from_run=inv.get("reused_from_run") or None,
            )
            for inv in (state.get("tool_invocations") or [])
        ])

        # Real-time per-claim cost snapshot. Computed AFTER the children are
        # written (so the breakdown reflects every LLM call this run made) but
        # BEFORE the atomic block closes — so cost lands in the same commit as
        # the rest of the run. Best-effort: if cost compute fails, the run
        # still persists with cost=0 and we log the exception.
        try:
            cost = _compute_run_cost(str(run.id))
            RuleExecutionRun.objects.filter(id=run.id).update(**cost)
            # Stamp on the in-memory state so the response carries it too.
            state.setdefault("cost", {}).update(cost)
        except Exception:  # pragma: no cover — cost must never break a run
            logger.exception("rule_engine: cost compute failed for run_id=%s",
                             run.id)


def _persist_trace(state: ExecutionState) -> None:
    """Additive: build + store the trace/explainability log for this run.

    Runs after the canonical run/evaluation/tool rows are written. Any failure
    here is swallowed by the caller so it can never affect the run outcome.
    """
    from execution_app.models import ClaimTrace, RuleExecutionRun
    from execution_app import trace_builder

    run = RuleExecutionRun.objects.filter(id=state["run_id"]).first()
    if run is None:
        return
    trace, explainability = trace_builder.build_trace(
        run,
        list(state.get("rule_results") or []),
        list(state.get("tool_invocations") or []),
    )
    # Store the *overall* claim audit status (CLEAN / DEFECT / INCONCLUSIVE),
    # not just the first agent's — the list view reads this directly. The
    # engine's aggregated verdict (final_decision_type) is authoritative: an
    # ALLOW means "no adverse disposition applied" → CLEAN, regardless of how
    # many intermediate sub-checks were Not-Met. Fall back to the per-step
    # trace rollup only when there is no decision (e.g. a fetch/exec failure).
    final_status = (
        trace_builder.normalize_decision(state.get("final_decision_type") or "")
        or trace_builder.claim_status(trace)
    )
    ClaimTrace.objects.update_or_create(
        run=run,
        defaults=dict(
            claim_id=run.claim_id or "",
            final_status=final_status,
            trace_json=trace,
            explainability_json=explainability,
        ),
    )


def _build_response(state: ExecutionState) -> dict[str, Any]:
    evals_out: list[dict] = []
    for ev in (state.get("rule_results") or []):
        evals_out.append({
            "rule_key": ev["rule_key"],
            "source": ev["source"],
            "shape_id": ev.get("shape_id", ""),
            "shape_label": ev.get("shape_label", ""),
            "matched": ev["matched"],
            "skipped": bool(ev.get("skipped")),
            "skip_reason": ev.get("skip_reason", ""),
            "decision_type": ev["decision_type"],
            "confidence": ev["confidence"],
            "reasoning": ev["reasoning"],
            "codes": ev["codes"],
            "eob_codes": ev.get("eob_codes") or [],
            "tool_results_used": ev["tool_results_used"],
        })
    tools_out = [{
        "tool": inv["tool_name"],
        "phase": inv.get("phase", "EVALUATE"),
        "ok": inv["ok"],
        "ms": inv["duration_ms"],
        "error": inv.get("error", ""),
    } for inv in (state.get("tool_invocations") or [])]

    status = state.get("status") or "COMPLETED"
    if status == "RUNNING":
        status = "COMPLETED"

    claim = state.get("claim") or {}
    excel_fields = {
        k: claim[k]
        for k in EXCEL_BILLING_FIELDS
        if claim.get(k) not in (None, "")
    }

    return {
        "run_id": state["run_id"],
        "claim_id": state.get("claim_id") or "",
        "status": status,
        "final_decision_type": state.get("final_decision_type") or "",
        "applied_codes": list(state.get("applied_codes") or []),
        "narrative": state.get("narrative") or "",
        "claim_lob": state.get("claim_lob") or {},
        "terminated_at_shape_id": state.get("terminated_at_shape_id") or "",
        "evaluations": evals_out,
        "tool_invocations": tools_out,
        "stages": list(state.get("stages") or []),
        "error_message": state.get("error_message") or "",
        # Per-claim LLM cost snapshot. ``cost`` is set in _persist after the run
        # children are written; ``{}`` for fail-open runs that never reached it.
        "cost": state.get("cost") or {},
        **excel_fields,
    }


def persist_and_respond(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    # Claim-level memory backstop: if the aggregated decision drifted from the
    # remembered one (prior_wins, unchanged claim data), adopt the prior
    # decision + narrative before anything persists, so the DB, trace, and
    # response all carry the adopted output.
    try:
        overrides = apply_claim_level_policy(get_config(), dict(state))
        if overrides:
            state = {**state, **overrides}
    except Exception:  # pragma: no cover - memory must never block a run
        logger.exception("rule_engine: claim-level policy failed for run_id=%s",
                         state.get("run_id"))
    try:
        _persist(state)
    except Exception as exc:
        logger.exception("rule_engine: persist failed for run_id=%s",
                         state.get("run_id"))
        # The atomic block in _persist rolled back the parent finalize, so
        # the row is either gone (if n01 never reserved it) or back in its
        # RUNNING state. Write a fresh FAILED record carrying the real
        # error so the failure surfaces in the DB instead of being hidden.
        try:
            from execution_app.models import RuleExecutionRun
            RuleExecutionRun.objects.filter(id=state.get("run_id")).update(
                status="FAILED",
                error_message=f"persist: {exc}"[:8000],
                finished_at=timezone.now(),
            )
        except Exception:  # pragma: no cover — recovery must not crash
            logger.exception("rule_engine: persist recovery update failed "
                             "for run_id=%s", state.get("run_id"))

        stages.append({"node": "persist_and_respond", "status": "FAIL",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": str(exc)})
        # Build a response anyway so the caller sees the in-memory result.
        synthetic = dict(state)
        synthetic["status"] = "FAILED"
        synthetic["error_message"] = f"persist: {exc}"
        synthetic["stages"] = stages
        return {"status": "FAILED",
                "error_message": f"persist: {exc}",
                "stages": stages,
                "response": _build_response(synthetic)}

    # Additive trace/explainability log. Best-effort: a failure here must
    # never roll back or fail the run that already persisted above.
    try:
        _persist_trace(state)
    except Exception:  # pragma: no cover - trace must never break a run
        logger.exception("rule_engine: trace persist failed for run_id=%s",
                         state.get("run_id"))

    # Persistent per-claim context. Best-effort for the same reason: the run
    # is already committed; a memory failure only means the next run starts
    # cold for this claim.
    try:
        update_claim_memory(get_config(), dict(state))
    except Exception:  # pragma: no cover - memory must never break a run
        logger.exception("rule_engine: claim memory update failed for run_id=%s",
                         state.get("run_id"))

    # Post-persist breadcrumb: this is the canonical "what was actually
    # written to the DB" line. Useful for grepping the log by run_id when
    # the API response looks wrong.
    final_status = state.get("status") or "COMPLETED"
    if final_status == "RUNNING":
        final_status = "COMPLETED"
    logger.info(
        "persist_and_respond run=%s claim=%s status=%s decision=%s "
        "evals=%d tools=%d codes=%s",
        state.get("run_id"), state.get("claim_id") or "-", final_status,
        state.get("final_decision_type") or "-",
        len(state.get("rule_results") or []),
        len(state.get("tool_invocations") or []),
        list(state.get("applied_codes") or []),
    )

    stages.append({"node": "persist_and_respond", "status": "OK",
                   "ms": int((time.time() - t0) * 1000)})
    updated = dict(state)
    updated["stages"] = stages
    return {"stages": stages, "response": _build_response(updated)}
