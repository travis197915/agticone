"""REST views for the rule execution agent.

POST /api/execute/workflows/<workflow_id>/run-batch/         multipart upload (sync)
POST /api/execute/workflows/<workflow_id>/run-batch-async/   multipart upload (async + SSE)
GET  /api/execute/batches/<batch_id>/                        prior batch result
GET  /api/execute/batches/<batch_id>/events/                 SSE stream of live batch events
GET  /api/execute/runs/<run_id>/                             single-claim audit trail
GET  /api/execute/runs/<run_id>/nodes/                       per-canvas-node rollup
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator

from django.conf import settings
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BatchExecutionRun, RuleExecutionRun
from .serializers import (BatchExecutionRunSerializer,
                           RuleExecutionRunSerializer)

logger = logging.getLogger(__name__)

# Heartbeat cadence for SSE — must stay under the proxy idle timeout
# (nginx default 60s, AWS ALB 60s, gunicorn `--timeout`).
_SSE_HEARTBEAT_SECONDS = 15

# Terminal SSE event kinds — when the bridge sees one of these, it closes
# the pubsub and returns. `summary` is the happy path, `error` is the
# task-level crash path.
_SSE_TERMINAL_KINDS = {"summary", "error"}


class RunBatchView(APIView):
    """POST /api/execute/workflows/<workflow_id>/run-batch/

    Multipart form: ``file`` (.xlsx, required), ``claim_id_column`` (optional),
    ``sheet_name`` (optional). Runs every claim in the Excel through the rule
    engine and returns a JSON batch summary. Synchronous for v1.
    """
    parser_classes = [MultiPartParser]
    permission_classes = [AllowAny]

    def post(self, request: Request, workflow_id: str) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "file (multipart) is required"},
                            status=status.HTTP_400_BAD_REQUEST)
        if not upload.name.lower().endswith(".xlsx"):
            return Response({"detail": "only .xlsx is supported"},
                            status=status.HTTP_400_BAD_REQUEST)

        from uhc_execution_engine import BatchRunner
        runner = BatchRunner()
        try:
            result = runner.run_xlsx(
                workflow_id=workflow_id,
                xlsx_bytes=upload.read(),
                filename=upload.name,
                claim_id_column=request.data.get("claim_id_column") or None,
                sheet_name=request.data.get("sheet_name") or None,
            )
        except Exception as exc:
            logger.exception("run-batch crashed")
            return Response(
                {"detail": f"engine crashed: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        http_status = status.HTTP_200_OK
        if result.get("status") == "FAILED":
            http_status = status.HTTP_400_BAD_REQUEST
        return Response(result, status=http_status)


class BatchDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, _request: Request, batch_id: str) -> Response:
        try:
            batch = BatchExecutionRun.objects.prefetch_related("runs").get(id=batch_id)
        except BatchExecutionRun.DoesNotExist:
            return Response({"detail": "not found"},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(BatchExecutionRunSerializer(batch).data)


class RunDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, _request: Request, run_id: str) -> Response:
        try:
            run = (RuleExecutionRun.objects
                   .prefetch_related("evaluations", "tool_invocations")
                   .get(id=run_id))
        except RuleExecutionRun.DoesNotExist:
            return Response({"detail": "not found"},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(RuleExecutionRunSerializer(run).data)


class RunNodesView(APIView):
    """GET /api/execute/runs/<run_id>/nodes/

    Per-canvas-node rollup for one claim's run. Walks the already-persisted
    ``RuleEvaluation`` + ``ToolInvocationRecord`` rows, groups them by the
    Shape that owned each binding, and returns one entry per node visited
    during the run — in the order the engine evaluated them.

    No new tables; this is purely a derived view.
    """
    permission_classes = [AllowAny]

    def get(self, _request: Request, run_id: str) -> Response:
        try:
            run = (RuleExecutionRun.objects
                   .select_related("workflow")
                   .prefetch_related(
                       "evaluations__rule_binding__shape",
                       "tool_invocations__tool_binding__shape",
                   )
                   .get(id=run_id))
        except RuleExecutionRun.DoesNotExist:
            return Response({"detail": "not found"},
                            status=status.HTTP_404_NOT_FOUND)

        # Insertion-ordered dict keyed by shape_id, so the response preserves
        # the order in which the engine first touched each node (driven by
        # RuleEvaluation.order_index, which is set in canvas order).
        nodes: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        outer_tools: list[dict[str, Any]] = []

        def _node_slot(shape_id: str, shape_label: str) -> dict[str, Any]:
            slot = nodes.get(shape_id)
            if slot is None:
                slot = {
                    "shape_id": shape_id,
                    "shape_label": shape_label,
                    "evaluations": [],
                    "tool_invocations": [],
                    "rules_evaluated": 0,
                    "rules_matched": 0,
                    "matched_decision_types": [],
                    "terminated_here": False,
                }
                nodes[shape_id] = slot
            elif shape_label and not slot["shape_label"]:
                slot["shape_label"] = shape_label
            return slot

        # Iterate evaluations in their persisted order. The shape_id on the
        # binding wins; if the binding was deleted (FK SET_NULL), we still
        # have a stable key via the captured shape_label or rule_key prefix.
        for ev in run.evaluations.all().order_by("order_index"):
            rb = ev.rule_binding  # may be None if the binding was deleted
            if rb is not None:
                shape_id = str(rb.shape_id)
                shape_label = (rb.shape.label or "") if rb.shape else ""
            else:
                shape_id = ""
                shape_label = ""
            # Fall back to whatever the engine captured at run time. We don't
            # store ev.shape_id on the model today, so use a synthetic key
            # built from rule_key when neither source is available.
            if not shape_id:
                shape_id = f"orphaned:{ev.rule_key}"
            slot = _node_slot(shape_id, shape_label)
            slot["evaluations"].append({
                "order_index":   ev.order_index,
                "rule_key":      ev.rule_key,
                "rule_source":   ev.rule_source,
                "condition":     ev.condition,
                "action":        ev.action,
                "matched":       ev.matched,
                "confidence":    ev.confidence,
                "reasoning":     ev.reasoning,
                "decision_type": ev.decision_type,
                "codes":         list(ev.codes or []),
                "llm_provider":  ev.llm_provider,
                "llm_ms":        ev.llm_ms,
            })
            slot["rules_evaluated"] += 1
            if ev.matched:
                slot["rules_matched"] += 1
                if ev.decision_type and ev.decision_type not in slot["matched_decision_types"]:
                    slot["matched_decision_types"].append(ev.decision_type)

        # Tool invocations: bucket the shape-scoped ones onto their node,
        # surface the outer FETCH/PARSE calls (no tool_binding) separately.
        for inv in run.tool_invocations.all().order_by("called_at"):
            tb = inv.tool_binding
            payload = {
                "tool_name":   inv.tool_name,
                "phase":       inv.phase,
                "ok":          inv.ok,
                "duration_ms": inv.duration_ms,
                "error":       inv.error,
                "called_at":   inv.called_at,
            }
            if tb is None or inv.phase in ("FETCH", "PARSE"):
                outer_tools.append(payload)
                continue
            shape_id = str(tb.shape_id)
            shape_label = (tb.shape.label or "") if tb.shape else ""
            slot = _node_slot(shape_id, shape_label)
            slot["tool_invocations"].append(payload)

        # Flag the node that triggered an early halt: walk the rollup we
        # just built and mark the first node whose matched-rule list
        # contains a DENY/STOP outcome.
        if run.status == "TERMINATED_EARLY":
            for slot in nodes.values():
                if any(e["matched"] and e["decision_type"] in {"DENY", "STOP"}
                       for e in slot["evaluations"]):
                    slot["terminated_here"] = True
                    break

        return Response({
            "run_id":              str(run.id),
            "workflow_id":         str(run.workflow_id),
            "claim_id":            run.claim_id,
            "status":              run.status,
            "final_decision_type": run.final_decision_type,
            "applied_codes":       list(run.applied_codes or []),
            "narrative":           run.narrative,
            "nodes":               list(nodes.values()),
            "outer_tool_invocations": outer_tools,
        })


# ── Streaming endpoints ──────────────────────────────────────────────────────


def _execution_upload_dir() -> Path:
    """Where the kickoff view stashes the .xlsx for the Celery task to read.

    Sits under MEDIA_ROOT when configured, otherwise under the system temp
    directory (matches Django's default upload behaviour).
    """
    media = getattr(settings, "MEDIA_ROOT", "") or ""
    base = Path(media) if media else Path("/tmp")
    target = base / "execution_uploads"
    target.mkdir(parents=True, exist_ok=True)
    return target


class RunBatchAsyncView(APIView):
    """POST /api/execute/workflows/<workflow_id>/run-batch-async/

    Multipart upload — same form fields as the sync ``RunBatchView``.
    Returns 202 immediately with::

        {
            "batch_id":   "<uuid>",
            "status":     "RUNNING",
            "stream_url": "/api/execute/batches/<id>/events/",
        }

    The actual per-claim work runs in the ``execution_app.run_batch_async``
    Celery task. The SPA subscribes to ``stream_url`` via EventSource to
    receive per-Shape / per-rule / per-claim events as they happen.
    """
    parser_classes = [MultiPartParser]
    permission_classes = [AllowAny]

    def post(self, request: Request, workflow_id: str) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "file (multipart) is required"},
                            status=status.HTTP_400_BAD_REQUEST)
        if not upload.name.lower().endswith(".xlsx"):
            return Response({"detail": "only .xlsx is supported"},
                            status=status.HTTP_400_BAD_REQUEST)

        batch_id = str(uuid.uuid4())

        # Stash the upload to disk; the Celery worker reads it and unlinks.
        xlsx_path = _execution_upload_dir() / f"{batch_id}.xlsx"
        try:
            with xlsx_path.open("wb") as fh:
                for chunk in upload.chunks():
                    fh.write(chunk)
        except OSError as exc:
            logger.exception("run-batch-async: could not stash upload")
            return Response(
                {"detail": f"failed to stash upload: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Reserve a RUNNING BatchExecutionRun row eagerly so the SPA's
        # 202 response carries a real batch_id it can subscribe to right
        # away. The Celery task will patch in source_filename / total /
        # final status once it parses the workbook.
        BatchExecutionRun.objects.create(
            id=batch_id,
            workflow_id=str(workflow_id),
            source_filename=upload.name,
            claim_id_column=str(request.data.get("claim_id_column") or "claim_id"),
            total_claims=0,
            status="RUNNING",
        )

        from .tasks import run_batch_async
        run_batch_async.delay(
            batch_id=batch_id,
            xlsx_path=str(xlsx_path),
            workflow_id=str(workflow_id),
            filename=upload.name,
            claim_id_column=request.data.get("claim_id_column") or None,
            sheet_name=request.data.get("sheet_name") or None,
        )

        return Response(
            {
                "batch_id":   batch_id,
                "status":     "RUNNING",
                "stream_url": f"/api/execute/batches/{batch_id}/events/",
            },
            status=status.HTTP_202_ACCEPTED,
        )


def _sse_format(event_kind: str, data: dict) -> bytes:
    """Format one SSE event. Always terminates with a blank line."""
    return (
        f"event: {event_kind}\n"
        f"data: {json.dumps(data, default=str)}\n\n"
    ).encode("utf-8")


def _claim_payload_from_run(run: RuleExecutionRun) -> dict:
    """Project a RuleExecutionRun row into a `claim` SSE payload."""
    return {
        "claim_id":               run.claim_id,
        "run_id":                 str(run.id),
        "status":                 run.status,
        "final_decision_type":    run.final_decision_type,
        "applied_codes":          list(run.applied_codes or []),
        "narrative":              run.narrative,
        "error_message":          run.error_message,
    }


class BatchEventsView(APIView):
    """GET /api/execute/batches/<batch_id>/events/

    Server-Sent Events stream. Subscribes to the Redis pub/sub channel
    ``batch:<batch_id>`` and pipes each published event to the SPA. On
    connect, replays any already-finished claims from the DB so reconnects
    pick up cleanly without the publisher having to retain events.

    Terminates when a ``summary`` or ``error`` event arrives (the task's
    final publish) or when the client disconnects.
    """
    permission_classes = [AllowAny]

    def get(self, _request: Request, batch_id: str) -> StreamingHttpResponse:
        # Existence check before we commit to streaming. 404 is meaningful
        # only here; once we're inside the SSE body, all errors are
        # delivered as `event: error`.
        try:
            batch = BatchExecutionRun.objects.get(id=batch_id)
        except BatchExecutionRun.DoesNotExist:
            return StreamingHttpResponse(
                iter([_sse_format("error", {
                    "batch_id": str(batch_id),
                    "message": "batch not found",
                })]),
                content_type="text/event-stream",
                status=status.HTTP_404_NOT_FOUND,
            )

        response = StreamingHttpResponse(
            self._iter_sse(batch),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-store"
        response["X-Accel-Buffering"] = "no"  # nginx: don't buffer
        response["Connection"] = "keep-alive"
        return response

    def _iter_sse(self, batch: BatchExecutionRun) -> Iterator[bytes]:
        """Generator that yields SSE-framed bytes until the batch is done."""
        # Lazy imports — Redis isn't needed for the model-existence check.
        try:
            from uhc_execution_engine.llm import _get_redis
            pubsub = _get_redis().pubsub(ignore_subscribe_messages=True)
        except Exception as exc:
            logger.exception("batch-events: could not init pubsub")
            yield _sse_format("error", {
                "batch_id": str(batch.id),
                "message": f"redis unavailable: {exc}",
            })
            return

        channel = f"batch:{batch.id}"
        emitted_run_ids: set[str] = set()
        try:
            # Subscribe FIRST so events published during the DB catch-up
            # window aren't lost. Pubsub buffers between subscribe and the
            # first get_message call.
            pubsub.subscribe(channel)

            # batch_start envelope from the DB (the Celery task will also
            # publish its own batch_start once it parses the workbook; the
            # SPA can dedupe on batch_id if it cares).
            yield _sse_format("batch_start", {
                "batch_id":        str(batch.id),
                "workflow_id":     str(batch.workflow_id),
                "total_claims":    batch.total_claims,
                "claim_id_column": batch.claim_id_column,
                "source_filename": batch.source_filename,
                "status":          batch.status,
            })

            # Catch-up: replay finished claim rows so a late subscriber
            # sees them as `claim` events.
            already_finished = RuleExecutionRun.objects.filter(
                batch_id=batch.id, finished_at__isnull=False,
            ).order_by("started_at")
            for run in already_finished:
                emitted_run_ids.add(str(run.id))
                yield _sse_format("claim", _claim_payload_from_run(run))

            # If the batch is already terminal before we connected, emit
            # a synthetic summary and bail — no point waiting for events
            # that will never come.
            if batch.status in {"COMPLETED", "PARTIAL", "FAILED"}:
                yield _sse_format("summary", {
                    "id":            str(batch.id),
                    "status":        batch.status,
                    "total_claims":  batch.total_claims,
                    "completed":     batch.completed,
                    "failed":        batch.failed,
                    "error_message": batch.error_message,
                })
                return

            # Live loop. Heartbeat every _SSE_HEARTBEAT_SECONDS to defeat
            # proxy idle timeouts.
            while True:
                msg = pubsub.get_message(timeout=_SSE_HEARTBEAT_SECONDS)
                if msg is None:
                    yield b": keepalive\n\n"
                    continue
                if msg.get("type") != "message":
                    continue
                try:
                    raw = msg.get("data")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    event = json.loads(raw)
                except (ValueError, TypeError) as exc:
                    logger.warning("batch-events: bad payload (%s): %r",
                                   exc, msg.get("data"))
                    continue
                kind = event.get("kind") or ""

                # Dedupe `claim` events against the catch-up set so a row
                # that just finished isn't emitted twice.
                if kind == "claim":
                    result = event.get("result") or {}
                    run_id = str(result.get("run_id") or "")
                    if run_id and run_id in emitted_run_ids:
                        continue
                    if run_id:
                        emitted_run_ids.add(run_id)
                    yield _sse_format("claim", result)
                    continue

                # batch_start from the task — skip; we already sent our
                # DB-derived one above.
                if kind == "batch_start":
                    continue

                # summary / error — terminal; emit and break.
                if kind == "summary":
                    yield _sse_format("summary", event.get("batch") or {})
                    break
                if kind == "error":
                    yield _sse_format("error", {
                        "batch_id": event.get("batch_id") or str(batch.id),
                        "message":  event.get("message") or "",
                    })
                    break

                # Pass-through for the engine's per-Shape / per-rule
                # events. Strip the envelope wrapper — SPA reads from
                # event.data directly.
                payload = {k: v for k, v in event.items() if k != "kind"}
                yield _sse_format(kind, payload)
        except GeneratorExit:
            # Client disconnected mid-stream. The Celery task is
            # unaffected; a reconnect will catch up via the DB read.
            logger.info("batch-events: client disconnected batch=%s", batch.id)
            raise
        except Exception as exc:
            logger.exception("batch-events: bridge crashed batch=%s", batch.id)
            yield _sse_format("error", {
                "batch_id": str(batch.id),
                "message":  f"bridge crashed: {exc}",
            })
        finally:
            try:
                pubsub.unsubscribe(channel)
                pubsub.close()
            except Exception as exc:  # pragma: no cover
                logger.warning("batch-events: pubsub teardown failed (%s)", exc)
