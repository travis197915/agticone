"""n01 — validate_input: sanity-check the request and stamp a run_id."""
from __future__ import annotations

import time
import uuid

from ..state import ExecutionState


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
    stages.append({"node": "validate_input", "status": "OK",
                   "ms": int((time.time() - t0) * 1000)})
    return {
        "run_id": run_id,
        "status": "RUNNING",
        "stages": stages,
    }
