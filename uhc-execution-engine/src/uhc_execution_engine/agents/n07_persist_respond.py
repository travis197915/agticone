"""n07 — persist_and_respond: write run + evaluation + tool rows; build response."""
from __future__ import annotations

import time
from typing import Any

from django.utils import timezone

from ..state import ExecutionState


def _persist(state: ExecutionState) -> None:
    from execution_app.models import (RuleExecutionRun, RuleEvaluation,
                                         ToolInvocationRecord)

    status = state.get("status") or "COMPLETED"
    if status == "RUNNING":
        status = "COMPLETED"

    # ``n01_validate`` reserved a RUNNING row with this run_id so that
    # LLMCallLog rows could FK to it during evaluation. Finalize it now.
    # update_or_create handles the rare case where validate failed before
    # reserving (e.g. missing workflow_id) and we still want a record.
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
            confidence=ev["confidence"],
            reasoning=ev["reasoning"],
            decision_type=ev["decision_type"],
            codes=ev["codes"],
            tool_results_used=ev["tool_results_used"],
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
        )
        for inv in (state.get("tool_invocations") or [])
    ])


def _build_response(state: ExecutionState) -> dict[str, Any]:
    evals_out: list[dict] = []
    for ev in (state.get("rule_results") or []):
        evals_out.append({
            "rule_key": ev["rule_key"],
            "source": ev["source"],
            "shape_id": ev.get("shape_id", ""),
            "shape_label": ev.get("shape_label", ""),
            "matched": ev["matched"],
            "decision_type": ev["decision_type"],
            "confidence": ev["confidence"],
            "reasoning": ev["reasoning"],
            "codes": ev["codes"],
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

    return {
        "run_id": state["run_id"],
        "claim_id": state.get("claim_id") or "",
        "status": status,
        "final_decision_type": state.get("final_decision_type") or "",
        "applied_codes": list(state.get("applied_codes") or []),
        "narrative": state.get("narrative") or "",
        "terminated_at_shape_id": state.get("terminated_at_shape_id") or "",
        "evaluations": evals_out,
        "tool_invocations": tools_out,
        "stages": list(state.get("stages") or []),
        "error_message": state.get("error_message") or "",
    }


def persist_and_respond(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    try:
        _persist(state)
    except Exception as exc:
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

    stages.append({"node": "persist_and_respond", "status": "OK",
                   "ms": int((time.time() - t0) * 1000)})
    updated = dict(state)
    updated["stages"] = stages
    return {"stages": stages, "response": _build_response(updated)}
