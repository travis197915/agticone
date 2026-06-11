"""
REST endpoints
--------------
GET  /api/ingest/health/         — package + Celery health check (no auth)
POST /api/ingest/                — start async ingestion job
GET  /api/ingest/                — list all jobs
GET  /api/ingest/<job_id>/       — poll a job
DEL  /api/ingest/<job_id>/       — delete a completed job
POST /api/ingest/run-sync/       — run pipeline inline (DEBUG only)

HTML viewer
-----------
GET  /api/ingest/viewer/                      — job list
GET  /api/ingest/viewer/<job_id>/             — job detail + logs
GET  /api/ingest/viewer/<job_id>/doc/<doc_id>/— full SOP document viewer
"""
from __future__ import annotations

import logging
from pathlib import Path

from django.shortcuts import get_object_or_404, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (IngestionJob, JobStatus,
                     AuditSop, AuditPrecondition, AuditStep, AuditDecision,
                     AuditGroupLimit, AuditCode, AuditDateCondition,
                     AuditAnnotation, AuditReference,
                     AuditGraphNode, AuditGraphEdge,
                     PipelineStageLog, LLMCallLog)
from .serializers import IngestionJobSerializer, StartJobSerializer
from .tasks import run_ingestion_pipeline, run_narrative_contextualizer

log = logging.getLogger(__name__)
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


# ── Health ────────────────────────────────────────────────────────────────────

class HealthView(APIView):
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        result: dict = {"status": "ok", "checks": {}}

        try:
            import uhc_sop_ingestion   # noqa: F401
            result["checks"]["uhc_sop_ingestion"] = "ok"
        except ImportError as exc:
            result["checks"]["uhc_sop_ingestion"] = f"MISSING — {exc}"
            result["status"] = "degraded"

        try:
            from celery import current_app
            ping = current_app.control.ping(timeout=1.0)
            result["checks"]["celery"] = "ok" if ping else "no_workers"
        except Exception as exc:
            result["checks"]["celery"] = f"error: {exc}"

        try:
            from .subprocess_manager import (
                active_subprocess_snapshot,
                max_subprocesses,
            )
            active = active_subprocess_snapshot()
            result["checks"]["ingestion_subprocesses"] = {
                "max": max_subprocesses(),
                "active_count": len(active),
                "jobs": active,
            }
        except Exception as exc:
            result["checks"]["ingestion_subprocesses"] = f"error: {exc}"

        result["env_file_found"] = _ENV_PATH.exists()
        result["jobs_total"]     = IngestionJob.objects.count()
        result["jobs_running"]   = IngestionJob.objects.filter(
                                       status=JobStatus.RUNNING).count()
        return Response(result)


# ── List + Create ─────────────────────────────────────────────────────────────

class JobListCreateView(APIView):

    def get(self, request: Request) -> Response:
        qs = IngestionJob.objects.prefetch_related("audit_sops").all()
        if sf := request.query_params.get("status"):
            qs = qs.filter(status=sf.upper())
        page      = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", 20))
        total     = qs.count()
        offset    = (page - 1) * page_size
        return Response({
            "count":   total,
            "page":    page,
            "results": IngestionJobSerializer(qs[offset:offset + page_size],
                                              many=True).data,
        })

    def post(self, request: Request) -> Response:
        ser = StartJobSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        d   = ser.validated_data
        job = IngestionJob.objects.create(
            seed_url     = d["seed_url"],
            max_depth    = d["max_depth"],
            max_docs     = d["max_docs"],
            llm_provider = d["llm_provider"],
            llm_model    = d["llm_model"],
        )
        task = run_ingestion_pipeline.delay(str(job.job_id))
        job.celery_task_id = task.id
        job.save(update_fields=["celery_task_id"])

        log.info("Job %s dispatched → Celery task %s", job.job_id, task.id)
        return Response(IngestionJobSerializer(job).data,
                        status=status.HTTP_202_ACCEPTED)


# ── Detail ────────────────────────────────────────────────────────────────────

class JobDetailView(APIView):

    def _job(self, job_id):
        try:
            return IngestionJob.objects.prefetch_related("audit_sops").get(pk=job_id)
        except (IngestionJob.DoesNotExist, ValueError):
            return None

    def get(self, request: Request, job_id: str) -> Response:
        job = self._job(job_id)
        if not job:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(IngestionJobSerializer(job).data)

    def delete(self, request: Request, job_id: str) -> Response:
        job = self._job(job_id)
        if not job:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if job.status == JobStatus.RUNNING:
            return Response({"detail": "Cannot delete a running job."},
                            status=status.HTTP_409_CONFLICT)
        job.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Sync run (dev only) ───────────────────────────────────────────────────────

class SyncRunView(APIView):
    """Runs the pipeline in-process — no Celery needed.
    Only available when DEBUG=True.
    """
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        from django.conf import settings as djsettings
        if not djsettings.DEBUG:
            return Response({"detail": "Only available in DEBUG mode."},
                            status=status.HTTP_403_FORBIDDEN)

        ser = StartJobSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        import os
        d   = ser.validated_data
        job = IngestionJob.objects.create(
            seed_url     = d["seed_url"],
            max_depth    = d["max_depth"],
            max_docs     = d["max_docs"],
            llm_provider = d["llm_provider"],
            llm_model    = d["llm_model"],
        )
        job.mark_started()

        # Mirror what tasks.py does: push LLM choice into env so
        # PipelineConfig.from_env() picks them up when the pipeline initialises.
        os.environ["LLM_PROVIDER"] = job.llm_provider
        os.environ["LLM_MODEL"]    = job.llm_model

        try:
            from uhc_sop_ingestion.pipeline import SopIngestionPipeline
            pipeline    = SopIngestionPipeline(
                env_path=_ENV_PATH if _ENV_PATH.exists() else None
            )
            final_state = pipeline.run(
                d["seed_url"],
                job_id=str(job.job_id),   # <-- must match the DB row created above
                max_depth=d["max_depth"],
                max_docs=d["max_docs"],
            )
        except Exception as exc:
            job.mark_failed(str(exc))
            return Response({"job_id": str(job.job_id), "status": "FAILED",
                             "error": str(exc)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        job.mark_done(final_state.get("final_summary") or {},
                      final_state.get("errors") or [])
        return Response(IngestionJobSerializer(job).data)


# ── Graph JSON (for embedding in the SPA) ────────────────────────────────────

class JobGraphView(APIView):
    """Return the knowledge-graph for the first/primary SOP in a job as JSON.

    Endpoint: ``GET /api/ingest/<job_id>/graph/``

    Response shape::

        {
          "sop_id":    22,
          "seed_url":  "https://...",
          "nodes":     [{ id, label, type, details, display_order }, ...],
          "edges":     [{ id, source, target, rel, label, details }, ...]
        }
    """
    permission_classes = [AllowAny]  # token already auth'd via Django session

    def get(self, request: Request, job_id: str) -> Response:
        job = get_object_or_404(IngestionJob, pk=job_id)
        sop = AuditSop.objects.filter(job=job).order_by("id").first()
        if not sop:
            return Response({
                "sop_id":   None,
                "seed_url": job.seed_url,
                "status":   job.status,
                "nodes":    [],
                "edges":    [],
            })

        nodes_qs = AuditGraphNode.objects.filter(sop=sop).order_by(
            "display_order", "id")
        edges_qs = AuditGraphEdge.objects.filter(sop=sop).select_related(
            "source", "target").order_by("id")

        nodes = [{
            "id":            n.node_key,
            "label":         n.label or n.node_key,
            "type":          n.node_type,
            "details":       n.details or {},
            "ref_table":     n.ref_table,
            "ref_id":        n.ref_id,
            "display_order": n.display_order,
        } for n in nodes_qs]

        edges = [{
            "id":       f"e{e.id}",
            "source":   e.source.node_key,
            "target":   e.target.node_key,
            "rel":      e.rel_type,
            "label":    e.label,
            "details":  e.details or {},
        } for e in edges_qs]

        return Response({
            "sop_id":   sop.id,
            "seed_url": job.seed_url,
            "status":   job.status,
            "nodes":    nodes,
            "edges":    edges,
        })


# ── Sections JSON (for embedding in the SPA) ─────────────────────────────────

class JobSectionsView(APIView):
    """Structured tabular view of an SOP for the React app.

    Endpoint: ``GET /api/ingest/<job_id>/sections/``

    Returns the SOP broken down by section: pre-conditions (with their
    extracted rules), decision tree steps (with If/Then rows), codes,
    group limits, date conditions, annotations, references.  Mirrors the
    HTML viewer at ``/api/ingest/viewer/<job_id>/doc/<doc_id>/`` but as
    pure JSON so the SPA can render whatever UI it likes.
    """
    permission_classes = [AllowAny]

    def get(self, request: Request, job_id: str) -> Response:
        job = get_object_or_404(IngestionJob, pk=job_id)
        sop = AuditSop.objects.filter(job=job).order_by("id").first()
        if not sop:
            return Response({
                "sop_id":        None,
                "seed_url":      job.seed_url,
                "status":        job.status,
                "title":         "",
                "preconditions": [],
                "steps":         [],
                "codes":         [],
                "group_limits":  [],
                "annotations":   [],
                "references":    [],
            })

        preconditions = [{
            "id":           pc.id,
            "order":        pc.display_order,
            "category":     pc.category,
            "label":        pc.label,
            "content_text": pc.content_text,
            "is_blocking":  pc.is_blocking,
            "rules":        pc.llm_rules or [],
        } for pc in sop.preconditions.all().order_by("display_order", "id")]

        def _serialize_decision(d, by_parent):
            """One decision row + its nested children (preserves YAML subrules)."""
            return {
                "row_index":       d.row_index,
                "depth":           d.depth,
                "subrule_id":      d.subrule_id,
                "table_name":      d.table_name,
                "aggregation":     d.aggregation,
                "condition_if":    d.condition_if,
                "condition_and":   d.condition_and,
                "action_text":     d.action_text,
                "action_summary":  d.action_summary,
                "output_text":     d.output_text,
                "decision_type":   d.decision_type,
                "tooling_allowed": d.tooling_allowed,
                "is_out_of_scope": d.is_out_of_scope,
                "goto_step":       d.goto_step,
                "is_final":        d.is_final,
                "eob_codes":       d.eob_codes or [],
                "ex_codes":        d.ex_codes or [],
                "denial_codes":    d.denial_codes or [],
                "system_actions":  d.system_actions or [],
                "all_codes":       d.all_codes or [],
                "children":        [
                    _serialize_decision(c, by_parent)
                    for c in by_parent.get(d.id, [])
                ],
            }

        steps = []
        steps_qs = sop.steps.prefetch_related("decisions").order_by("step_number")
        for step in steps_qs:
            # Group decisions by parent so we can rebuild the tree in one pass.
            by_parent: dict = {}
            for d in step.decisions.all().order_by("depth", "row_index"):
                by_parent.setdefault(d.parent_id, []).append(d)
            # Top-level rows (parent_id is None) carry the nested children.
            decisions = [
                _serialize_decision(d, by_parent)
                for d in by_parent.get(None, [])
            ]
            steps.append({
                "step_number":      step.step_number,
                "yaml_rule_id":     step.yaml_rule_id,
                "question":         step.question,
                "intro_text":       step.intro_text,
                "narrative":        step.narrative_context,
                "is_terminal":      step.is_terminal,
                "terminal_action":  step.terminal_action,
                "is_sub_procedure": step.is_sub_procedure,
                "sub_procedure":    step.sub_procedure_name,
                "is_out_of_scope":  step.is_out_of_scope,
                "decisions":        decisions,
            })

        codes = [{
            "value":       c.code_value,
            "type":        c.code_type,
            "description": c.description,
            "context":     c.context_snippet,
            "source_step": c.source_step,
        } for c in sop.codes.all().order_by("code_type", "code_value")]

        group_limits = [{
            "group_name":     g.group_name,
            "inn_days":       g.inn_days,
            "oon_days":       g.oon_days,
            "limit_days":     g.limit_days,
            "limit_months":   g.limit_months,
            "limit_years":    g.limit_years,
            "basis":          g.calculation_basis,
            "network_type":   g.network_type,
            "exceptions":     g.exceptions or [],
            "special_notes":  g.special_notes or [],
        } for g in sop.group_limits.all().order_by("group_name")]

        annotations = [{
            "type":            a.annotation_type,
            "content_text":    a.content_text,
            "is_claim_impact": a.is_claim_impact,
            "step_number":     a.step.step_number if a.step_id else None,
        } for a in sop.annotations.all().order_by("annotation_type")]

        references = [{
            "ref_text":    r.ref_text,
            "ref_url":     r.ref_url,
            "ref_type":    r.ref_type,
            "is_resolved": r.is_resolved,
            "step_number": r.step.step_number if r.step_id else None,
        } for r in sop.references.all().order_by("ref_type")]

        return Response({
            "sop_id":        sop.id,
            "seed_url":      job.seed_url,
            "status":        job.status,
            "title":         sop.title,
            "purpose":       sop.purpose,
            "summary":       sop.llm_summary,
            "narrative":     sop.narrative_context,
            "platform":      sop.platform,
            "lob":           sop.lob or [],
            "preconditions": preconditions,
            "steps":         steps,
            "codes":         codes,
            "group_limits":  group_limits,
            "annotations":   annotations,
            "references":    references,
        })


# ── Narrative contextualizer (backfill) ──────────────────────────────────────

class JobContextualizeView(APIView):
    """Trigger the narrative agents on every SOP in a job.

    ``POST /api/ingest/<job_id>/contextualize/`` — Re-runs only the
    narrative stage of the pipeline on already-ingested SOPs.  Useful
    for SOPs that were ingested before the narrative stage existed.

    Query/body params:
      ``sync=true``  — run inline and return the result.
                       Otherwise dispatches a Celery task and returns 202.
    """
    permission_classes = [AllowAny]

    def post(self, request: Request, job_id: str) -> Response:
        job = get_object_or_404(IngestionJob, pk=job_id)
        run_sync = (
            str(request.query_params.get("sync", "")).lower() in {"1", "true", "yes"}
            or bool(request.data.get("sync"))
        )

        if run_sync:
            from .services.contextualizer import contextualize_job
            try:
                results = contextualize_job(str(job.job_id))
                return Response({"job_id": str(job.job_id), "results": results})
            except Exception as exc:
                log.exception("Sync contextualize failed for job %s", job_id)
                return Response(
                    {"error": str(exc)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        task = run_narrative_contextualizer.delay(str(job.job_id))
        return Response(
            {"job_id": str(job.job_id), "celery_task_id": task.id},
            status=status.HTTP_202_ACCEPTED,
        )


# ── HTML Viewer ───────────────────────────────────────────────────────────────

class ViewerJobListView(View):
    def get(self, request):
        current_status = request.GET.get("status", "ALL").upper()
        qs = IngestionJob.objects.all()
        if current_status != "ALL":
            qs = qs.filter(status=current_status)
        jobs_data = []
        for job in qs:
            dur = None
            if job.started_at and job.completed_at:
                dur = round((job.completed_at - job.started_at).total_seconds(), 1)
            jobs_data.append({
                "job_id": job.job_id, "seed_url": job.seed_url,
                "status": job.status, "docs_processed": job.docs_processed,
                "total_llm_calls": job.total_llm_calls,
                "total_tokens_in": job.total_tokens_in,
                "total_tokens_out": job.total_tokens_out,
                "llm_provider": job.llm_provider, "llm_model": job.llm_model,
                "created_at": job.created_at, "duration_seconds": dur,
                "summary": job.summary or {},
            })
        status_filters = [
            ("ALL", "All"), ("COMPLETED", "Completed"),
            ("RUNNING", "Running"), ("FAILED", "Failed"), ("QUEUED", "Queued"),
        ]
        return render(request, "sop_ingestion/viewer_jobs.html", {
            "jobs": jobs_data, "total": IngestionJob.objects.count(),
            "current_status": current_status, "status_filters": status_filters,
        })


class ViewerJobDetailView(View):
    def get(self, request, job_id):
        job = get_object_or_404(IngestionJob, pk=job_id)
        dur = None
        if job.started_at and job.completed_at:
            dur = round((job.completed_at - job.started_at).total_seconds(), 1)
        job_ctx = {
            "job_id": job.job_id, "seed_url": job.seed_url, "status": job.status,
            "llm_provider": job.llm_provider, "llm_model": job.llm_model,
            "total_llm_calls": job.total_llm_calls,
            "total_tokens_in": job.total_tokens_in, "total_tokens_out": job.total_tokens_out,
            "duration_seconds": dur, "summary": job.summary or {},
        }
        audit_sops = AuditSop.objects.filter(job=job).order_by("crawl_depth", "id")
        stage_logs = PipelineStageLog.objects.filter(job=job).order_by("started_at")
        llm_logs   = LLMCallLog.objects.filter(job=job).order_by("called_at")
        return render(request, "sop_ingestion/viewer_job.html", {
            "job": job_ctx, "sop_docs": audit_sops,
            "stage_logs": stage_logs, "llm_logs": llm_logs,
        })


@method_decorator(xframe_options_exempt, name="dispatch")
class ViewerDocDetailView(View):
    """
    Claims audit SOP viewer — shows the SOP exactly as a human auditor would use it.
    Organised into:
      1. Pre-conditions (what to check before entering the decision tree)
      2. Decision tree (steps and If/Then rules)
      3. Claims codes reference (EOB, EX, denial, system actions)
      4. Group limits (timely filing by group)
      5. Date conditions and annotations
      6. Cross-references
    """
    def get(self, request, job_id, doc_id):
        job = get_object_or_404(IngestionJob, pk=job_id)
        doc = get_object_or_404(AuditSop, pk=doc_id, job=job)

        # Pre-conditions the auditor checks first
        preconditions = AuditPrecondition.objects.filter(sop=doc).order_by("display_order")

        # Decision tree: steps with their If/Then rows
        steps_qs = AuditStep.objects.filter(sop=doc).prefetch_related("decisions").order_by("step_number")
        steps = []
        for step in steps_qs:
            decisions = list(step.decisions.all().order_by("row_index"))
            steps.append({
                "step": step,
                "decisions": decisions,
                "decision_count": len(decisions),
            })

        # Claims codes reference
        codes           = AuditCode.objects.filter(sop=doc).order_by("code_type", "code_value")
        group_limits    = AuditGroupLimit.objects.filter(sop=doc).order_by("group_name")
        date_conditions = AuditDateCondition.objects.filter(sop=doc)
        annotations     = AuditAnnotation.objects.filter(sop=doc).order_by("annotation_type")
        references      = AuditReference.objects.filter(sop=doc).order_by("ref_type")

        # Count decisions across all steps for tab badge
        total_decisions = sum(s["decision_count"] for s in steps)

        # Group codes by type for display
        codes_by_type: dict = {}
        for c in codes:
            codes_by_type.setdefault(c.code_type, []).append(c)

        # ── Cytoscape.js knowledge-graph data — read from PERSISTED tables ────
        # Single source of truth: sop_ingestion_auditgraphnode + ...graphedge.
        # The same graph is mirrored to Neo4j by a10_write_neo4j.neo4j_graph_writer.
        # ----------------------------------------------------------------------
        import json as _json

        g_nodes_qs = AuditGraphNode.objects.filter(sop=doc).order_by(
            "display_order", "id")
        g_edges_qs = AuditGraphEdge.objects.filter(sop=doc).select_related(
            "source", "target").order_by("id")

        def _label(n: AuditGraphNode) -> str:
            l = n.label or n.node_key
            return l if len(l) <= 80 else l[:78].rstrip() + "…"

        nodes = [{
            "data": {
                "id": n.node_key,
                "label": _label(n),
                "type": n.node_type,
                "details": n.details or {},
                "ref_table": n.ref_table,
                "ref_id": n.ref_id,
                "display_order": n.display_order,
            }
        } for n in g_nodes_qs]

        edges = [{
            "data": {
                "id": f"e{e.id}",
                "source": e.source.node_key,
                "target": e.target.node_key,
                "rel": e.rel_type,
                "label": e.label,
                "details": e.details or {},
            }
        } for e in g_edges_qs]

        graph_data_json = _json.dumps({"nodes": nodes, "edges": edges})
        graph_node_count = len(nodes)
        graph_edge_count = len(edges)

        tabs = [
            ("graph",         "Knowledge Graph",   graph_node_count),
            ("preconditions", "Pre-Conditions",    preconditions.count()),
            ("steps",         "Decision Tree",     len(steps)),
            ("decisions",     "All Decisions",     total_decisions),
            ("codes",         "Claims Codes",      codes.count()),
            ("groups",        "Group Limits",      group_limits.count()),
            ("dates",         "Date Conditions",   date_conditions.count()),
            ("annotations",   "Notes & Alerts",    annotations.count()),
            ("references",    "References",        references.count()),
            ("raw",           "Raw Text",          None),
        ]
        return render(request, "sop_ingestion/viewer_doc.html", {
            "job": {"job_id": job.job_id}, "doc": doc,
            "preconditions": preconditions,
            "steps": steps,
            "codes": codes, "codes_by_type": codes_by_type,
            "group_limits": group_limits,
            "date_conditions": date_conditions,
            "annotations": annotations,
            "references": references,
            "tabs": tabs,
            "graph_data_json": graph_data_json,
            "graph_node_count": graph_node_count,
            "graph_edge_count": graph_edge_count,
        })
