#!/usr/bin/env python
"""Subprocess entrypoint: run one execution batch by batch_id.

Usage (from repo root, PYTHONPATH=.):

    python -m execution_app.worker.batch_runner \\
        --batch-id <uuid> --xlsx-path <path> --workflow-id <uuid> \\
        --filename <name> [--claim-id-column <col>] [--sheet-name <sheet>]

The Celery master task ``execution_app.run_batch_async`` spawns this script
via ``execution_app.subprocess_manager.spawn_execution_subprocess``; it is
not invoked by hand in normal ops.

Mirrors ``sop_ingestion.worker.job_runner``: the subprocess owns the heavy
work (BatchRunner.iter_xlsx → LangGraph → DB writes → Redis pub/sub for
SSE), so a crash here does not poison the Celery worker.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="execution_app.worker.batch_runner")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--xlsx-path", required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--filename", required=True)
    parser.add_argument("--claim-id-column", default=None)
    parser.add_argument("--sheet-name", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sop_backend.settings")

    import django
    django.setup()

    # Pull settings.py's LOGGING config in (uhc_execution_engine + execution_app
    # already wired to console + rotating file there). Do this after
    # django.setup() so the dictConfig has been applied.
    log = logging.getLogger("execution_app.worker.batch_runner")
    log.info(
        "batch_runner start batch=%s workflow=%s xlsx=%s filename=%s",
        args.batch_id, args.workflow_id, args.xlsx_path, args.filename,
    )

    xlsx_path = Path(args.xlsx_path)
    if not xlsx_path.exists():
        log.error("xlsx not found at %s — marking batch FAILED", xlsx_path)
        _mark_batch_failed(args.batch_id, f"upload not found at {xlsx_path}")
        return 2

    try:
        xlsx_bytes = xlsx_path.read_bytes()
    except OSError as exc:
        log.exception("could not read xlsx %s", xlsx_path)
        _mark_batch_failed(args.batch_id, f"could not read upload: {exc}")
        return 2

    try:
        from uhc_execution_engine import BatchRunner
        from uhc_execution_engine.llm import batch_context

        runner = BatchRunner()
        with batch_context(args.batch_id):
            # Drive the generator to completion. iter_xlsx itself updates
            # BatchExecutionRun + RuleExecutionRun rows + publishes SSE
            # events on the batch_id Redis channel as it goes — we just
            # need to consume it.
            for event in runner.iter_xlsx(
                workflow_id=args.workflow_id,
                xlsx_bytes=xlsx_bytes,
                filename=args.filename,
                claim_id_column=args.claim_id_column,
                sheet_name=args.sheet_name,
                batch_id=args.batch_id,
            ):
                # The engine itself does not republish summary/claim/batch_start
                # envelopes to Redis — that was the Celery task's job in the
                # in-process design. Now we own it.
                _publish_envelope(args.batch_id, event)
    except Exception as exc:
        log.exception("batch_runner crashed batch=%s", args.batch_id)
        _publish_envelope(args.batch_id, {
            "kind": "error",
            "batch_id": args.batch_id,
            "message": str(exc),
        })
        _mark_batch_failed(args.batch_id, str(exc))
        return 1
    finally:
        # Clean up the temp upload regardless of outcome.
        try:
            if xlsx_path.exists():
                os.unlink(xlsx_path)
        except OSError as exc:  # pragma: no cover
            log.warning("could not delete %s (%s)", xlsx_path, exc)

    log.info("batch_runner done batch=%s", args.batch_id)
    return 0


def _publish_envelope(batch_id: str, payload: dict) -> None:
    """Best-effort Redis publish; mirrors the old Celery task's helper."""
    import json
    log = logging.getLogger("execution_app.worker.batch_runner")
    try:
        from uhc_execution_engine.llm import _get_redis
        _get_redis().publish(f"batch:{batch_id}", json.dumps(payload, default=str))
    except Exception as exc:  # pragma: no cover — telemetry must not abort
        log.warning("publish failed batch=%s: %s", batch_id, exc)


def _mark_batch_failed(batch_id: str, error_message: str) -> None:
    """Best-effort finalize of the BatchExecutionRun row on fatal error."""
    log = logging.getLogger("execution_app.worker.batch_runner")
    try:
        from django.utils import timezone
        from execution_app.models import BatchExecutionRun
        BatchExecutionRun.objects.filter(id=batch_id).update(
            status="FAILED",
            error_message=error_message[:8000],
            finished_at=timezone.now(),
        )
    except Exception as exc:  # pragma: no cover
        log.warning("could not mark batch %s FAILED: %s", batch_id, exc)


if __name__ == "__main__":
    raise SystemExit(main())
