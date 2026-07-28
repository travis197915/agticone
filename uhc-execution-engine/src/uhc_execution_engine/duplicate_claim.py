"""Skip duplicate claims during batch processing."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)

SKIP_REASON_BATCH_DUPLICATE = "duplicate_in_batch"
SKIP_REASON_PRIOR_CLEAN = "prior_clean_run"

CLAIM_PAYLOAD_SKIP_REASON_KEY = "_skip_reason"
CLAIM_PAYLOAD_REUSED_RUN_KEY = "_reused_from_run_id"

_TERMINAL_RUN_STATUSES = frozenset({
    "COMPLETED", "TERMINATED_EARLY", "FAILED", "FETCH_FAILED", "SKIPPED",
})


def _skip_message(skip_reason: str, reused_from_run_id: str) -> str:
    if skip_reason == SKIP_REASON_BATCH_DUPLICATE:
        return (
            f"Skipped: duplicate claim in batch (reused run {reused_from_run_id})"
        )
    if skip_reason == SKIP_REASON_PRIOR_CLEAN:
        return (
            f"Skipped: prior CLEAN run exists (reused run {reused_from_run_id})"
        )
    return f"Skipped: {skip_reason} (reused run {reused_from_run_id})"


def _prior_fields(prior: Any) -> dict[str, Any]:
    """Normalize a prior ``RuleExecutionRun`` or per-claim result dict."""
    if hasattr(prior, "id"):
        reused_from_run_id = str(prior.id)
        return {
            "reused_from_run_id": reused_from_run_id,
            "final_decision_type": prior.final_decision_type or "",
            "applied_codes": list(prior.applied_codes or []),
            "narrative": prior.narrative or "",
            "claim_lob": dict(prior.claim_lob or {}),
        }
    reused_from_run_id = (
        str(prior.get("reused_from_run_id") or prior.get("run_id") or "")
    )
    return {
        "reused_from_run_id": reused_from_run_id,
        "final_decision_type": prior.get("final_decision_type") or "",
        "applied_codes": list(prior.get("applied_codes") or []),
        "narrative": prior.get("narrative") or "",
        "claim_lob": dict(prior.get("claim_lob") or {}),
    }


def find_prior_clean_run(
    *,
    claim_id: str,
    workflow_id: str,
    exclude_batch_id: str | None,
) -> Any | None:
    """Return the newest terminal run for this claim that audited CLEAN."""
    if not claim_id:
        return None
    try:
        from execution_app.serializers import claim_audit_status
        from execution_app.models import RuleExecutionRun
        from execution_app.trace_builder import CLEAN

        qs = (
            RuleExecutionRun.objects.filter(
                claim_id=claim_id,
                workflow_id=workflow_id,
            )
            .exclude(status="RUNNING")
            .select_related("trace")
            .order_by("-finished_at", "-started_at")
        )
        if exclude_batch_id:
            qs = qs.exclude(batch_id=exclude_batch_id)

        for run in qs[:50]:
            if run.status not in _TERMINAL_RUN_STATUSES:
                continue
            if claim_audit_status(run) == CLEAN:
                return run
        return None
    except Exception:
        logger.exception(
            "duplicate_claim: prior CLEAN lookup failed claim=%s workflow=%s",
            claim_id,
            workflow_id,
        )
        return None


def record_skipped_claim(
    *,
    batch_id: str,
    workflow_id: str,
    claim_id: str,
    excel_payload: dict[str, Any],
    prior: Any,
    skip_reason: str,
    run_id: str | None = None,
    original_auditor: str = "",
    auditor_status: str = "",
) -> dict[str, Any]:
    """Persist a SKIPPED run row and return the per-claim response dict."""
    from execution_app.models import RuleExecutionRun

    fields = _prior_fields(prior)
    reused_from_run_id = fields["reused_from_run_id"]
    run_id = run_id or str(uuid.uuid4())
    claim_payload = {
        **excel_payload,
        CLAIM_PAYLOAD_SKIP_REASON_KEY: skip_reason,
        CLAIM_PAYLOAD_REUSED_RUN_KEY: reused_from_run_id,
    }
    RuleExecutionRun.objects.create(
        id=run_id,
        batch_id=batch_id,
        workflow_id=workflow_id,
        claim_id=claim_id,
        claim_payload=claim_payload,
        raw_fetch={},
        finished_at=timezone.now(),
        status="SKIPPED",
        final_decision_type=fields["final_decision_type"],
        applied_codes=fields["applied_codes"],
        narrative=fields["narrative"],
        claim_lob=fields["claim_lob"],
        error_message=_skip_message(skip_reason, reused_from_run_id),
        original_auditor=original_auditor,
        auditor_status=auditor_status,
    )
    logger.info(
        "batch=%s claim=%s skipped reason=%s reused_run=%s",
        batch_id,
        claim_id,
        skip_reason,
        reused_from_run_id,
    )
    return {
        "run_id": run_id,
        "claim_id": claim_id,
        "status": "SKIPPED",
        "skip_reason": skip_reason,
        "reused_from_run_id": reused_from_run_id,
        "final_decision_type": fields["final_decision_type"],
        "applied_codes": fields["applied_codes"],
        "narrative": fields["narrative"],
        "claim_lob": fields["claim_lob"],
        "error_message": _skip_message(skip_reason, reused_from_run_id),
        "tool_invocations": [],
        **excel_payload,
    }


def skip_metadata(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Expose skip markers stored on ``claim_payload``."""
    if not payload:
        return {}
    out: dict[str, Any] = {}
    reason = payload.get(CLAIM_PAYLOAD_SKIP_REASON_KEY)
    reused = payload.get(CLAIM_PAYLOAD_REUSED_RUN_KEY)
    if reason:
        out["skip_reason"] = reason
    if reused:
        out["reused_from_run_id"] = reused
    return out
