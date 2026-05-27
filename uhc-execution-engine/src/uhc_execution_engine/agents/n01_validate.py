"""n01 — validate_input: sanity-check the request and reserve the run row.

We pre-create the ``RuleExecutionRun`` row here (status=RUNNING) so that
LLMCallLog rows written by downstream nodes can FK back to it via
``execution_run``. ``n07_persist_respond`` later finalises the row with the
verdict and any evaluation/tool-invocation children.
"""
from __future__ import annotations

import logging
import time
import uuid

from ..state import ExecutionState

logger = logging.getLogger(__name__)


def _reserve_run_row(*, run_id: str, workflow_id: str,
                     claim: dict, raw_fetch: dict,
                     claim_id: str, batch_id: str | None) -> None:
    """Insert a RUNNING RuleExecutionRun row so child FKs are valid early."""
    try:
        from execution_app.models import RuleExecutionRun
        RuleExecutionRun.objects.create(
            id=run_id,
            batch_id=batch_id or None,
            workflow_id=workflow_id,
            claim_id=claim_id,
            claim_payload=claim,
            raw_fetch=raw_fetch,
            status="RUNNING",
        )
    except Exception as exc:  # pragma: no cover — non-fatal
        logger.warning("validate_input: could not pre-create run row (%s)", exc)


def validate_input(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    claim = state.get("claim") or {}
    workflow_id = state.get("workflow_id") or ""

    if not workflow_id:
        stages.append({"node": "validate_input", "status": "FAIL",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": "workflow_id is required"})
        return {"status": "FAILED", "error_message": "workflow_id is required",
                "stages": stages}
    if not isinstance(claim, dict) or not claim:
        stages.append({"node": "validate_input", "status": "FAIL",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": "claim payload is required"})
        return {"status": "FAILED", "error_message": "claim payload is required",
                "stages": stages}

    run_id = state.get("run_id") or str(uuid.uuid4())
    _reserve_run_row(
        run_id=run_id,
        workflow_id=workflow_id,
        claim=claim,
        raw_fetch=state.get("raw_fetch") or {},
        claim_id=state.get("claim_id") or "",
        batch_id=state.get("batch_id"),
    )

    stages.append({"node": "validate_input", "status": "OK",
                   "ms": int((time.time() - t0) * 1000)})
    return {
        "run_id": run_id,
        "status": "RUNNING",
        "stages": stages,
    }
