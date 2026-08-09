"""Re-run claims against the workflow's current rules.

A reprocess is an ordinary execution — live tool calls, every rule evaluated,
the engine's own aggregation — producing a **new** ``RuleExecutionRun``. The
previous run is left untouched, so the pair can be compared and the listing can
show which rule version each used.

The whole batch pipeline is driven by an uploaded ``.xlsx``: view → Celery task
→ subprocess → ``batch_runner --xlsx-path``. Rather than add a parallel
claim-id path through four layers, this rebuilds the spreadsheet rows from the
``claim_payload`` the original run stored and hands them to the existing
kickoff. The pipeline cannot tell the difference, which is the point — no
second execution path to keep faithful.

The reconstruction is faithful because ``claim_payload`` *is* the parsed
spreadsheet row: ``xlsx_parser`` reads a claim id plus ``paid_dt`` /
``total_billed`` / ``total_paid``, and the auditor columns land on the run
itself as ``original_auditor`` / ``auditor_status``.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

__all__ = ["ReprocessError", "build_reprocess_workbook", "dispatch_reprocess"]

# Header names chosen from the aliases ``xlsx_parser`` accepts, so the rebuilt
# sheet parses back to the same fields it came from.
_CLAIM_ID_HEADER = "claim_id"
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("paid_dt", "paid_dt"),
    ("total_billed", "total_billed"),
    ("total_paid", "total_paid"),
)
_AUDITOR_HEADER = "auditorname"
_AUDIT_STATUS_HEADER = "audit_sts"


class ReprocessError(RuntimeError):
    """Nothing dispatchable — the caller should surface this, not retry."""


def build_reprocess_workbook(runs: list, dest: Path) -> int:
    """Write one row per run, rebuilt from its stored claim payload.

    Returns the number of rows written. Claims are de-duplicated: asking to
    reprocess two runs of the same claim should queue it once, or the batch's
    own duplicate guard would skip the second and report a confusing "skipped"
    against a claim the user explicitly selected.
    """
    from openpyxl import Workbook

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for run in runs:
        claim_id = (run.claim_id or "").strip()
        if not claim_id or claim_id in seen:
            continue
        seen.add(claim_id)
        payload = run.claim_payload or {}
        row = {_CLAIM_ID_HEADER: claim_id}
        for source, header in _COLUMNS:
            value = payload.get(source)
            if value not in (None, ""):
                row[header] = value
        if run.original_auditor:
            row[_AUDITOR_HEADER] = run.original_auditor
        if run.auditor_status:
            row[_AUDIT_STATUS_HEADER] = run.auditor_status
        rows.append(row)

    if not rows:
        raise ReprocessError("No claims to reprocess.")

    headers: list[str] = [_CLAIM_ID_HEADER]
    for _source, header in _COLUMNS:
        if any(header in r for r in rows):
            headers.append(header)
    for header in (_AUDITOR_HEADER, _AUDIT_STATUS_HEADER):
        if any(header in r for r in rows):
            headers.append(header)

    book = Workbook()
    sheet = book.active
    sheet.title = "claims"
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(h, "") for h in headers])
    dest.parent.mkdir(parents=True, exist_ok=True)
    book.save(dest)
    return len(rows)


def dispatch_reprocess(*, runs: list, workflow_id: str, upload_dir: Path,
                       requested_by: str = "") -> dict[str, Any]:
    """Queue a reprocess batch for ``runs``. Returns the batch handle."""
    from execution_app.models import BatchExecutionRun
    from execution_app.tasks import run_batch_async

    batch_id = str(uuid.uuid4())
    xlsx_path = upload_dir / f"{batch_id}.xlsx"
    claim_count = build_reprocess_workbook(runs, xlsx_path)

    filename = f"reprocess-{claim_count}-claims.xlsx"
    BatchExecutionRun.objects.create(
        id=batch_id,
        workflow_id=str(workflow_id),
        source_filename=filename,
        claim_id_column=_CLAIM_ID_HEADER,
        total_claims=claim_count,
        status="RUNNING",
    )
    run_batch_async.delay(
        batch_id=batch_id,
        xlsx_path=str(xlsx_path),
        workflow_id=str(workflow_id),
        filename=filename,
        claim_id_column=_CLAIM_ID_HEADER,
        sheet_name=None,
    )
    logger.info(
        "reprocess dispatched batch=%s workflow=%s claims=%d by=%s",
        batch_id, workflow_id, claim_count, requested_by or "unknown",
    )
    return {
        "batch_id": batch_id,
        "claim_count": claim_count,
        "status": "RUNNING",
        "stream_url": f"/api/execute/batches/{batch_id}/events/",
    }


def runs_for_reprocess(run_ids: Iterable[str]) -> list:
    """Load the runs to reprocess, rejecting a mixed-workflow selection.

    One batch targets one workflow, so a selection spanning several cannot be
    dispatched as a unit — better to say so than to silently run the first.
    """
    from execution_app.models import RuleExecutionRun

    runs = list(
        RuleExecutionRun.objects.filter(id__in=list(run_ids))
        .only("id", "claim_id", "claim_payload", "workflow_id",
              "original_auditor", "auditor_status")
    )
    if not runs:
        raise ReprocessError("No matching runs.")
    workflows = {str(r.workflow_id) for r in runs}
    if len(workflows) > 1:
        raise ReprocessError(
            "Selected claims span more than one workflow; reprocess them "
            "one workflow at a time."
        )
    return runs
