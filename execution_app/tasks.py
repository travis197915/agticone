"""Celery master tasks for the execution engine — dispatch only.

Mirrors ``sop_ingestion.tasks``:

* :func:`run_batch_async` — master dispatcher. Validates that the
  ``BatchExecutionRun`` row exists, then spawns an OS subprocess
  (``execution_app.worker.batch_runner``) to actually run
  ``BatchRunner.iter_xlsx`` against the uploaded xlsx. The subprocess
  publishes SSE events to ``batch:<batch_id>`` on Redis exactly as
  before; the ``BatchEventsView`` (SSE bridge) is unchanged.

  This task does **not** run the LangGraph pipeline in-process. It
  returns immediately after Popen so the Celery prefork slot is freed.
"""
from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=0, name="execution_app.run_batch_async")
def run_batch_async(
    self,
    *,
    batch_id: str,
    xlsx_path: str,
    workflow_id: str,
    filename: str = "claims.xlsx",
    claim_id_column: str | None = None,
    sheet_name: str | None = None,
) -> dict:
    """Master dispatcher: spawn the batch-runner subprocess, return immediately.

    The kickoff view (``RunBatchAsyncView`` or ``RunBatchView``) has already:
      * created the ``BatchExecutionRun`` row (status=RUNNING),
      * stashed the uploaded workbook to ``xlsx_path``.

    Returns ``{batch_id, spawned, subprocess_pid, celery_task_id}`` on
    success — same shape as ``sop_ingestion.run_pipeline``.
    """
    from execution_app.models import BatchExecutionRun
    from execution_app.subprocess_manager import spawn_execution_subprocess

    try:
        BatchExecutionRun.objects.get(pk=batch_id)
    except BatchExecutionRun.DoesNotExist:
        log.error("Batch %s not found — not spawning subprocess", batch_id)
        return {"batch_id": batch_id, "error": "batch not found"}

    try:
        pid = spawn_execution_subprocess(
            batch_id=batch_id,
            xlsx_path=xlsx_path,
            workflow_id=workflow_id,
            filename=filename,
            claim_id_column=claim_id_column,
            sheet_name=sheet_name,
        )
    except Exception as exc:
        log.exception("Subprocess dispatch failed for batch %s", batch_id)
        try:
            from django.utils import timezone
            BatchExecutionRun.objects.filter(id=batch_id).update(
                status="FAILED",
                error_message=f"Subprocess dispatch failed: {exc}"[:8000],
                finished_at=timezone.now(),
            )
        except Exception:  # pragma: no cover — recovery must not crash
            log.exception("Could not mark batch %s FAILED after spawn failure",
                          batch_id)
        return {"batch_id": batch_id, "error": str(exc)}

    log.info(
        "Dispatched batch %s to subprocess pid=%s (celery_task=%s)",
        batch_id, pid, self.request.id,
    )
    return {
        "batch_id":        batch_id,
        "spawned":         True,
        "subprocess_pid":  pid,
        "celery_task_id":  self.request.id,
    }
