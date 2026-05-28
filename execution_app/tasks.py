"""Celery tasks for the execution engine.

Today there is one task:

* :func:`run_batch_async` — async sibling of the synchronous
  ``RunBatchView`` flow.  Drives ``BatchRunner.iter_xlsx`` inside a
  ``batch_context(batch_id)`` so per-Shape and per-rule events are
  published to the Redis pub/sub channel ``batch:<batch_id>``.  The
  ``BatchEventsView`` (SSE bridge) subscribes to that channel and pipes
  events to the SPA.  This task also publishes ``batch_start``,
  ``claim``, ``summary``, and ``error`` envelopes — the engine itself
  only publishes ``shape_start`` and ``rule_evaluated`` (see
  ``uhc_execution_engine.agents.n_execute_shapes``).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from celery import shared_task

log = logging.getLogger(__name__)


def _publish_envelope(batch_id: str, payload: dict) -> None:
    """Best-effort Redis publish; failures are logged and swallowed.

    Uses the same module-scope client + REDIS_URL discovery as the engine's
    ``uhc_execution_engine.llm.publish_event`` so a Redis outage degrades
    the task identically to a per-rule event drop.
    """
    try:
        from uhc_execution_engine.llm import _get_redis
        _get_redis().publish(f"batch:{batch_id}", json.dumps(payload, default=str))
    except Exception as exc:  # pragma: no cover — telemetry must not abort
        log.warning("run_batch_async: publish failed (%s)", exc)


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
    """Run a batch and publish per-claim + per-rule events to Redis.

    The kickoff view (``RunBatchAsyncView``) has already:
      * created the ``BatchExecutionRun`` row (status=RUNNING),
      * stashed the uploaded workbook to ``xlsx_path``.

    This task:
      1. Reads the workbook bytes from disk.
      2. Wraps ``BatchRunner.iter_xlsx`` in ``batch_context(batch_id)``
         so the engine's ``shape_start`` / ``rule_evaluated`` publishes
         route to ``batch:<batch_id>``.
      3. Re-publishes the generator's ``batch_start`` / ``claim`` /
         ``summary`` envelopes on the same channel.
      4. Deletes the temp file on the way out.

    On unhandled exception: publishes a final ``error`` envelope, sets
    ``BatchExecutionRun.status="FAILED"``, re-raises so Celery records
    the traceback.
    """
    # Local imports keep the task module importable when the engine
    # package isn't installed (e.g. lightweight migrations checks).
    from uhc_execution_engine import BatchRunner
    from uhc_execution_engine.llm import batch_context
    from execution_app.models import BatchExecutionRun
    from django.utils import timezone

    path = Path(xlsx_path)
    try:
        try:
            xlsx_bytes = path.read_bytes()
        except FileNotFoundError as exc:
            _publish_envelope(batch_id, {
                "kind": "error", "batch_id": batch_id,
                "message": f"upload not found at {xlsx_path}: {exc}",
            })
            BatchExecutionRun.objects.filter(id=batch_id).update(
                status="FAILED",
                error_message=str(exc),
                finished_at=timezone.now(),
            )
            return {"batch_id": batch_id, "status": "FAILED",
                    "error": str(exc)}

        runner = BatchRunner()
        try:
            with batch_context(batch_id):
                for event in runner.iter_xlsx(
                    workflow_id=workflow_id,
                    xlsx_bytes=xlsx_bytes,
                    filename=filename,
                    claim_id_column=claim_id_column,
                    sheet_name=sheet_name,
                    batch_id=batch_id,
                ):
                    _publish_envelope(batch_id, event)
        except Exception as exc:
            log.exception("run_batch_async crashed batch=%s", batch_id)
            _publish_envelope(batch_id, {
                "kind": "error",
                "batch_id": batch_id,
                "message": str(exc),
            })
            BatchExecutionRun.objects.filter(id=batch_id).update(
                status="FAILED",
                error_message=str(exc),
                finished_at=timezone.now(),
            )
            raise

        return {"batch_id": batch_id, "status": "OK"}
    finally:
        # Clean up the temp upload regardless of outcome.
        try:
            if path.exists():
                os.unlink(path)
        except OSError as exc:  # pragma: no cover
            log.warning("run_batch_async: could not delete %s (%s)",
                        xlsx_path, exc)
