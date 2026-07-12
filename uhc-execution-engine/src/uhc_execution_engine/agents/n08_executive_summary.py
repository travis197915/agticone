"""n08 — executive_summary (add-on): condense the run into a human-auditor blob.

Runs after ``persist_and_respond`` so the canonical run / evaluation rows are
already in the DB. Delegates to ``execution_app.executive_summary`` (which reads
those rows) and writes one ``ClaimExecutiveSummary`` row.

Purely additive and best-effort: the run has already persisted and its response
is built, so any failure here is logged and swallowed — it must never change the
claim outcome.
"""
from __future__ import annotations

import logging
import time

from ..state import ExecutionState

logger = logging.getLogger(__name__)


def executive_summary(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    run_id = state.get("run_id")
    try:
        from execution_app.executive_summary import generate_for_run
        from execution_app.models import RuleExecutionRun

        run = RuleExecutionRun.objects.filter(id=run_id).first()
        if run is None:
            stages.append({"node": "executive_summary", "status": "SKIP",
                           "ms": int((time.time() - t0) * 1000),
                           "msg": "run not found"})
            return {"stages": stages}
        generate_for_run(run, source="agent", force=True)
        stages.append({"node": "executive_summary", "status": "OK",
                       "ms": int((time.time() - t0) * 1000)})
    except Exception as exc:  # pragma: no cover - summary must never break a run
        logger.exception("rule_engine: executive_summary failed run=%s", run_id)
        stages.append({"node": "executive_summary", "status": "FAIL",
                       "ms": int((time.time() - t0) * 1000), "msg": str(exc)})
    return {"stages": stages}
