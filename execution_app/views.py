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
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic as _monotonic
from typing import Any, Iterator

from django.conf import settings
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.renderers import BaseRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from . import trace_builder
from .models import BatchExecutionRun, RuleExecutionRun
from .serializers import (BatchExecutionRunSerializer,
                          RuleExecutionRunSerializer)
from .trace_builder import CLEAN, DEFECT, INCONCLUSIVE, _DEFECT_DECISIONS

logger = logging.getLogger(__name__)

# Heartbeat cadence for SSE — must stay under the proxy idle timeout
# (nginx default 60s, AWS ALB 60s, gunicorn `--timeout`).
_SSE_HEARTBEAT_SECONDS = 15

# Terminal SSE event kinds — when the bridge sees one of these, it closes
# the pubsub and returns. `summary` is the happy path, `error` is the
# task-level crash path.
_SSE_TERMINAL_KINDS = {"summary", "error"}


def _iso_utc(ts: datetime | None) -> str | None:
    """Return an ISO-8601 UTC timestamp with Z suffix."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _format_clock(ts: datetime | None) -> str:
    """Return the UI-friendly clock string (UTC), e.g. 06:08:09 AM."""
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%I:%M:%S %p")


def _format_duration(duration_ms: int | None) -> str:
    """Return human-friendly duration: 12s, 1m 04s."""
    total_seconds = max(0, int((duration_ms or 0) / 1000))
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _relay_error(
    message: str,
    *,
    status_code: int,
    details: dict[str, Any] | None = None,
    source: str = "django",
) -> Response:
    payload: dict[str, Any] = {"error": message, "source": source}
    if details:
        payload["details"] = details
    return Response(payload, status=status_code)


def _build_node_rollup(run: RuleExecutionRun) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build per-node and outer-tool rollups for one execution run."""
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
            "order_index": ev.order_index,
            "rule_key": ev.rule_key,
            "rule_source": ev.rule_source,
            "condition": ev.condition,
            "action": ev.action,
            "matched": ev.matched,
            "skipped": getattr(ev, "skipped", False),
            "skip_reason": getattr(ev, "skip_reason", ""),
            "confidence": ev.confidence,
            "reasoning": ev.reasoning,
            "decision_type": ev.decision_type,
            "codes": list(ev.codes or []),
            "llm_provider": ev.llm_provider,
            "llm_ms": ev.llm_ms,
        })
        if not getattr(ev, "skipped", False):
            slot["rules_evaluated"] += 1
        if ev.matched and not getattr(ev, "skipped", False):
            slot["rules_matched"] += 1
            if ev.decision_type and ev.decision_type not in slot["matched_decision_types"]:
                slot["matched_decision_types"].append(ev.decision_type)

    # Tool invocations: bucket the shape-scoped ones onto their node,
    # surface the outer FETCH/PARSE calls (no tool_binding) separately.
    for inv in run.tool_invocations.all().order_by("called_at"):
        tb = inv.tool_binding
        payload = {
            "tool_name": inv.tool_name,
            "phase": inv.phase,
            "ok": inv.ok,
            "duration_ms": inv.duration_ms,
            "error": inv.error,
            "called_at": inv.called_at,
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

    return list(nodes.values()), outer_tools


def _trace_status_by_shape(trace) -> dict[str, str]:
    """shape_id -> aggregated audit status from the stored trace steps.

    The trace is the most faithful per-step audit signal (it carries the LLM's
    Met/Not-Met verdicts), so the agent chip should agree with the
    Explainability view. Returns an empty map when no trace exists.
    """
    out: dict[str, list[str]] = {}
    if trace is None:
        return {}
    for step in (trace.trace_json or []):
        sid = str(step.get("shape_id") or "")
        if not sid:
            continue
        out.setdefault(sid, []).append(str(step.get("status") or ""))
    return {sid: trace_builder.aggregate_status(sts) for sid, sts in out.items()}


def _agent_status(node: dict[str, Any], trace_by_shape: dict[str, str] | None = None) -> str:
    """3-state audit status for one node (CLEAN / DEFECT / INCONCLUSIVE).

    Prefers the trace-derived status so the agent chip matches the
    Explainability tab; falls back to matched + decision type for runs that
    predate the trace.
    """
    if trace_by_shape:
        st = trace_by_shape.get(node["shape_id"])
        if st:
            return st
    matched = [e for e in node["evaluations"] if e.get("matched")]
    if node.get("terminated_here") or any(
        (e.get("decision_type") or "").upper() in _DEFECT_DECISIONS for e in matched
    ):
        return DEFECT
    if matched:
        return CLEAN
    return INCONCLUSIVE


def _claim_status(
    run: RuleExecutionRun,
    nodes: list[dict[str, Any]],
    trace=None,
) -> str:
    """3-state claim audit status (CLEAN / DEFECT / INCONCLUSIVE).

    A system/fetch failure is *inconclusive* (the claim could not be audited),
    not a claim defect. When a trace exists we trust its aggregate so the header
    agrees with the per-agent / Explainability views.
    """
    if run.status == "RUNNING":
        return INCONCLUSIVE
    if run.status in {"FAILED", "FETCH_FAILED"}:
        return INCONCLUSIVE
    if trace is not None and trace.trace_json:
        return trace_builder.claim_status(trace.trace_json)
    if run.status == "TERMINATED_EARLY":
        return DEFECT
    decision = trace_builder.normalize_decision(run.final_decision_type)
    if decision:
        return decision
    statuses = [_agent_status(node) for node in nodes]
    return trace_builder.aggregate_status(statuses)


def _processing_time_min(run: RuleExecutionRun) -> float | None:
    if run.finished_at is None:
        return None
    return round((run.finished_at - run.started_at).total_seconds() / 60.0, 2)


def _serialize_agent(
    node: dict[str, Any],
    run: RuleExecutionRun,
    trace_by_shape: dict[str, str] | None = None,
) -> dict[str, Any]:
    invocations = node["tool_invocations"]
    begin_ts = invocations[0]["called_at"] if invocations else run.started_at
    end_ts = invocations[-1]["called_at"] if invocations else (run.finished_at or run.started_at)
    duration_sec = max(0, int((end_ts - begin_ts).total_seconds()))
    steps = []
    for idx, inv in enumerate(invocations, start=1):
        details = f"Called {inv['tool_name']} for claim {run.claim_id}"
        if inv.get("error"):
            details = f"{details}. Error: {inv['error']}"
        steps.append({
            "id": f"s{idx}",
            "name": inv["tool_name"],
            "status": "completed" if inv["ok"] else "failed",
            "duration": _format_duration(inv["duration_ms"]),
            "details": details,
        })
    process_summary = []
    for evaluation in node["evaluations"]:
        reasoning = (evaluation.get("reasoning") or "").strip()
        if reasoning:
            process_summary.append(reasoning)
    return {
        "id": node["shape_id"],
        "agentName": node["shape_label"] or node["shape_id"],
        "status": _agent_status(node, trace_by_shape),
        "beginTime": _format_clock(begin_ts),
        "endTime": _format_clock(end_ts),
        "durationSec": duration_sec,
        "processSummary": process_summary[:10],
        "steps": steps,
    }


_TERMINAL_BATCH_STATUSES = {"COMPLETED", "PARTIAL", "FAILED"}


def _dispatch_batch(
    *,
    request: Request,
    workflow_id: str,
) -> tuple[Response, str | None]:
    """Shared kickoff: validate upload → stash xlsx → reserve BatchExecutionRun
    → dispatch the Celery master task → return (error_response_or_None, batch_id).

    On success returns ``(None, batch_id)``. On validation failure returns
    ``(Response(4xx/5xx), None)`` — callers should just forward that response.
    """
    upload = request.FILES.get("file")
    if upload is None:
        return Response({"detail": "file (multipart) is required"},
                        status=status.HTTP_400_BAD_REQUEST), None
    if not upload.name.lower().endswith(".xlsx"):
        return Response({"detail": "only .xlsx is supported"},
                        status=status.HTTP_400_BAD_REQUEST), None

    batch_id = str(uuid.uuid4())

    xlsx_path = _execution_upload_dir() / f"{batch_id}.xlsx"
    try:
        with xlsx_path.open("wb") as fh:
            for chunk in upload.chunks():
                fh.write(chunk)
    except OSError as exc:
        logger.exception("run-batch: could not stash upload")
        return Response(
            {"detail": f"failed to stash upload: {exc}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        ), None

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
    logger.info("run-batch dispatched batch=%s workflow=%s file=%s",
                batch_id, workflow_id, upload.name)
    return None, batch_id


class RunBatchView(APIView):
    """POST /api/execute/workflows/<workflow_id>/run-batch/

    Multipart form: ``file`` (.xlsx, required), ``claim_id_column`` (optional),
    ``sheet_name`` (optional). Dispatches the batch through Celery to a
    dedicated OS subprocess (same path as ``/run-batch-async/``), then
    polls the ``BatchExecutionRun`` row until it reaches a terminal state
    and returns the aggregated batch dict from the DB.

    HTTP response is still synchronous — caller blocks until the batch
    finishes — but the work no longer runs inside the gunicorn worker.
    """
    parser_classes = [MultiPartParser]
    permission_classes = [AllowAny]

    # Bound on how long the sync HTTP request will wait. Overridable via env
    # for CI/large batches; align with the gunicorn / proxy timeout in prod.
    _DEFAULT_TIMEOUT_SEC = 600
    _POLL_INTERVAL_SEC = 0.5

    def post(self, request: Request, workflow_id: str) -> Response:
        err, batch_id = _dispatch_batch(request=request, workflow_id=workflow_id)
        if err is not None:
            return err

        timeout = float(os.environ.get(
            "RUN_BATCH_SYNC_TIMEOUT_SEC", str(self._DEFAULT_TIMEOUT_SEC)))
        deadline = _monotonic() + timeout

        # Poll the BatchExecutionRun row until terminal.
        while True:
            try:
                batch = BatchExecutionRun.objects.prefetch_related("runs").get(id=batch_id)
            except BatchExecutionRun.DoesNotExist:
                # Shouldn't happen — _dispatch_batch just created it.
                logger.error("run-batch: batch row %s disappeared mid-poll", batch_id)
                return Response(
                    {"detail": "batch row disappeared during run"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            if batch.status in _TERMINAL_BATCH_STATUSES:
                payload = BatchExecutionRunSerializer(batch).data
                # Preserve the response shape the legacy in-process runner
                # returned: top-level batch fields + a `results` list of
                # per-claim dicts.
                results = []
                for run in batch.runs.all().order_by("started_at"):
                    results.append({
                        "run_id":              str(run.id),
                        "claim_id":            run.claim_id,
                        "status":              run.status,
                        "final_decision_type": run.final_decision_type,
                        "applied_codes":       list(run.applied_codes or []),
                        "narrative":           run.narrative,
                        "error_message":       run.error_message,
                    })
                response_body = {
                    "batch_id":      str(batch.id),
                    "status":        batch.status,
                    "total_claims":  batch.total_claims,
                    "completed":     batch.completed,
                    "failed":        batch.failed,
                    "error_message": batch.error_message,
                    "results":       results,
                }
                http_status = (status.HTTP_400_BAD_REQUEST
                               if batch.status == "FAILED"
                               else status.HTTP_200_OK)
                return Response(response_body, status=http_status)

            if _monotonic() >= deadline:
                logger.warning(
                    "run-batch: timed out waiting on batch=%s after %ss "
                    "(status=%s); returning 504 — work continues in the background",
                    batch_id, timeout, batch.status,
                )
                return Response(
                    {
                        "detail":     "batch is still running; subscribe to the stream "
                                       "URL or poll GET /api/execute/batches/<id>/",
                        "batch_id":   batch_id,
                        "status":     batch.status,
                        "stream_url": f"/api/execute/batches/{batch_id}/events/",
                    },
                    status=status.HTTP_504_GATEWAY_TIMEOUT,
                )

            time.sleep(self._POLL_INTERVAL_SEC)


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
        nodes, outer_tools = _build_node_rollup(run)

        return Response({
            "run_id":              str(run.id),
            "workflow_id":         str(run.workflow_id),
            "claim_id":            run.claim_id,
            "status":              run.status,
            "final_decision_type": run.final_decision_type,
            "applied_codes":       list(run.applied_codes or []),
            "narrative":           run.narrative,
            "nodes":               nodes,
            "outer_tool_invocations": outer_tools,
        })


class ClaimProcessingView(APIView):
    """GET /api/claims/<claim_id>/processing/ aggregated claim processing snapshot."""
    permission_classes = [AllowAny]

    def get(self, _request: Request, claim_id: str) -> Response:
        run_id_param = (_request.query_params.get("run_id") or "").strip()
        batch_id_param = (_request.query_params.get("batch_id") or "").strip()

        run_uuid: uuid.UUID | None = None
        batch_uuid: uuid.UUID | None = None
        if run_id_param:
            try:
                run_uuid = uuid.UUID(run_id_param)
            except ValueError:
                return _relay_error(
                    "Malformed run_id query parameter",
                    status_code=status.HTTP_400_BAD_REQUEST,
                    details={"run_id": run_id_param},
                )
        if batch_id_param:
            try:
                batch_uuid = uuid.UUID(batch_id_param)
            except ValueError:
                return _relay_error(
                    "Malformed batch_id query parameter",
                    status_code=status.HTTP_400_BAD_REQUEST,
                    details={"batch_id": batch_id_param},
                )

        try:
            if run_uuid is not None:
                run = (RuleExecutionRun.objects
                       .select_related("workflow", "batch")
                       .prefetch_related(
                           "evaluations__rule_binding__shape",
                           "tool_invocations__tool_binding__shape",
                       )
                       .get(id=run_uuid))
            else:
                queryset = (RuleExecutionRun.objects
                            .select_related("workflow", "batch")
                            .prefetch_related(
                                "evaluations__rule_binding__shape",
                                "tool_invocations__tool_binding__shape",
                            )
                            .filter(claim_id=claim_id))
                if batch_uuid is not None:
                    queryset = queryset.filter(batch_id=batch_uuid)
                run = queryset.order_by("-started_at").first()
                if run is None:
                    return _relay_error(
                        f"No run found for claim {claim_id}",
                        status_code=status.HTTP_404_NOT_FOUND,
                    )
        except RuleExecutionRun.DoesNotExist:
            return _relay_error(
                f"No run found for claim {claim_id}",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except Exception as exc:
            logger.exception("claim-processing failed claim_id=%s", claim_id)
            return _relay_error(
                "Unexpected error while loading claim processing",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                details={"message": str(exc)},
            )

        nodes, outer_tools = _build_node_rollup(run)

        # Trace drives the canonical 3-state status so the claim header, the
        # agent chips and the Explainability tab all agree. Best-effort: a
        # missing/old trace just falls back to the node + decision derivation.
        from .models import ClaimTrace
        trace = ClaimTrace.objects.filter(run=run).first()
        trace_by_shape = _trace_status_by_shape(trace)

        # LLM-call telemetry — sourced from sop_ingestion.LLMCallLog where
        # _log_llm_call inserts one row per attempt, stamped with
        # execution_run_id by the ContextVar set in RuleEnginePipeline.run.
        # Survives a RuleEvaluation persist failure because LLMCallLog
        # rows are written inline by the LLM helper, not inside n07's
        # atomic block.
        from sop_ingestion.models import LLMCallLog
        llm_calls = list(
            LLMCallLog.objects.filter(execution_run_id=run.id).order_by("id")
        )

        payload = {
            "claimId": run.claim_id,
            "runId": str(run.id),
            "batchId": str(run.batch_id) if run.batch_id else None,
            "workflowId": str(run.workflow_id),
            "claimStatus": _claim_status(run, nodes, trace),
            # Engine-level run state — useful when claimStatus=DEFECT and the
            # SPA needs to render why. `runStatus` is the raw RuleExecutionRun
            # state (FAILED / FETCH_FAILED / TERMINATED_EARLY / COMPLETED /
            # RUNNING); `errorMessage` carries the n07 recovery-handler
            # message when a persist or fetch step crashed, otherwise empty.
            "runStatus": run.status,
            "errorMessage": run.error_message or "",
            "finalDecisionType": run.final_decision_type or "",
            "appliedCodes": list(run.applied_codes or []),
            "narrative": run.narrative or "",
            "processingTimeMin": _processing_time_min(run),
            "startedAt": _iso_utc(run.started_at),
            "finishedAt": _iso_utc(run.finished_at),
            "agents": [_serialize_agent(node, run, trace_by_shape) for node in nodes],
            "outerToolInvocations": [
                {
                    "phase": inv["phase"],
                    "tool": inv["tool_name"],
                    "status": "completed" if inv["ok"] else "failed",
                    "durationMs": inv["duration_ms"],
                }
                for inv in outer_tools
            ],
            "llmCalls": [
                {
                    "stage":            log.stage,
                    "agentName":        log.agent_name,
                    "provider":         log.llm_provider,
                    "model":            log.llm_model,
                    "promptTokens":     log.prompt_tokens,
                    "completionTokens": log.completion_tokens,
                    "totalTokens":      log.total_tokens,
                    "durationMs":       log.duration_ms,
                    "success":          log.success,
                    "error":            log.error_message,
                    "calledAt":         _iso_utc(log.called_at),
                }
                for log in llm_calls
            ],
            "reviewStatus": None,
            "feedback": None,
        }
        return Response(payload, status=status.HTTP_200_OK)


class ClaimTraceView(APIView):
    """GET /api/claims/<claim_id>/trace/ and /explainability/.

    Additive endpoints serving the denormalized ``ClaimTrace`` arrays in the
    ``trace.json`` / ``explainability.json`` shapes. ``?run_id=`` overrides the
    claim lookup; ``?batch_id=`` scopes it; ``?download=1`` returns the JSON as
    a file attachment. ``kind`` is set per URL route ("trace" | "explainability").
    """
    permission_classes = [AllowAny]
    kind = "trace"

    def get(self, request: Request, claim_id: str) -> Response:
        from django.http import JsonResponse

        from .models import ClaimTrace

        run_id_param = (request.query_params.get("run_id") or "").strip()
        batch_id_param = (request.query_params.get("batch_id") or "").strip()
        download = (request.query_params.get("download") or "").strip() in {"1", "true", "yes"}

        run_uuid: uuid.UUID | None = None
        batch_uuid: uuid.UUID | None = None
        if run_id_param:
            try:
                run_uuid = uuid.UUID(run_id_param)
            except ValueError:
                return _relay_error("Malformed run_id query parameter",
                                    status_code=status.HTTP_400_BAD_REQUEST,
                                    details={"run_id": run_id_param})
        if batch_id_param:
            try:
                batch_uuid = uuid.UUID(batch_id_param)
            except ValueError:
                return _relay_error("Malformed batch_id query parameter",
                                    status_code=status.HTTP_400_BAD_REQUEST,
                                    details={"batch_id": batch_id_param})

        qs = ClaimTrace.objects.select_related("run")
        if run_uuid is not None:
            trace = qs.filter(run_id=run_uuid).first()
        else:
            qs = qs.filter(claim_id=claim_id)
            if batch_uuid is not None:
                qs = qs.filter(run__batch_id=batch_uuid)
            trace = qs.order_by("-created_at").first()

        if trace is None:
            return _relay_error(
                f"No trace found for claim {claim_id}",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        data = trace.explainability_json if self.kind == "explainability" else trace.trace_json
        data = data or []

        if download:
            resp = JsonResponse(data, safe=False, json_dumps_params={"indent": 2})
            fname = f"{self.kind}_{trace.claim_id or claim_id}.json"
            resp["Content-Disposition"] = f'attachment; filename="{fname}"'
            return resp
        return Response(data, status=status.HTTP_200_OK)


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
        err, batch_id = _dispatch_batch(request=request, workflow_id=workflow_id)
        if err is not None:
            return err
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


class _EventStreamRenderer(BaseRenderer):
    """No-op renderer that advertises ``text/event-stream``.

    Exists solely to satisfy DRF's content negotiation for SSE endpoints.
    ``EventSource`` always sends ``Accept: text/event-stream``; with only
    ``JSONRenderer`` registered project-wide, the negotiator would 406 the
    request before our ``get()`` could return a ``StreamingHttpResponse``.

    ``render()`` is never invoked because the view returns a
    ``StreamingHttpResponse`` directly — DRF only renders ``Response``
    objects.
    """
    media_type = "text/event-stream"
    format = "txt"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):  # pragma: no cover
        return data


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
    renderer_classes = [_EventStreamRenderer]

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
        # NOTE: `Connection: keep-alive` is a hop-by-hop header (RFC 7230 §6.1)
        # — WSGI applications must not emit it; the server manages it. Setting
        # it here crashes wsgiref/runserver with AssertionError and is a no-op
        # under gunicorn (which already keeps HTTP/1.1 connections alive).
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
