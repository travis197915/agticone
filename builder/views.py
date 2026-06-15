"""
REST views for the builder.

Exposes:
  • catalog (shape categories + shapes), sidebar nav, dashboard widgets
  • workflow CRUD + duplicate / activate / deactivate
  • atomic graph bulk save / load
  • flat inspector CRUD for workbenches + shapes
"""
from __future__ import annotations

import html as _html
import os
import re
import unicodedata
import uuid
from copy import deepcopy
from pathlib import Path

from django.conf import settings as djsettings
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import (
    DashboardWidget,
    NavItem,
    Shape,
    ShapeCategory,
    ShapeDefinition,
    Workbench,
    Workflow,
)


_SOP_UPLOAD_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls",
                          ".html", ".htm"}


def _sop_upload_dir() -> Path:
    """Directory where uploaded SOP documents are stashed for ingestion.

    Sits under ``MEDIA_ROOT`` when configured, else under ``BASE_DIR``. The
    ingestion subprocess reads these via a ``file://`` seed URL, so the path
    must be readable by both the web process and the Celery workers.
    """
    media = getattr(djsettings, "MEDIA_ROOT", "") or ""
    base = Path(media) if media else Path(djsettings.BASE_DIR)
    target = base / "sop_uploads"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _text_to_basic_html(text: str) -> str:
    """Render plain SOP text into safe minimal HTML for the SPA's reference panel.

    Escapes HTML, converts paragraph breaks (blank line) to <p>, and bullet
    lines starting with "•", "-" or "*" into <ul><li> blocks. No external
    sanitiser dependency; output is intentionally tag-restricted.
    """
    if not text:
        return ""
    paragraphs = re.split(r"\n{2,}", text.strip())
    out: list[str] = []
    for p in paragraphs:
        lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
        is_bullet = lines and all(re.match(r"^[\u2022\u2023\-*]\s*", ln) for ln in lines)
        if is_bullet:
            items = [
                f"<li>{_html.escape(re.sub(r'^[\u2022\u2023\-*]\s*', '', ln))}</li>"
                for ln in lines
            ]
            out.append("<ul>" + "".join(items) + "</ul>")
        else:
            joined = " ".join(lines)
            out.append(f"<p>{_html.escape(joined)}</p>")
    return "".join(out)
from .serializers import (
    DashboardWidgetSerializer,
    NavItemSerializer,
    ShapeCategorySerializer,
    ShapeDefinitionSerializer,
    WorkflowGraphSerializer,
    WorkflowSerializer,
)
from .services import WorkflowGraphWriter
from .attachments import attach_to_workflow
from sop_ir.normalize import extract_all_gotos, extract_goto


# ── helpers ─────────────────────────────────────────────────────────────────


def _slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value or "workflow"


def _unique_workflow_slug(base: str, ignore_id=None) -> str:
    root = _slugify(base)
    candidate = root
    i = 1
    while True:
        qs = Workflow.objects.filter(slug=candidate)
        if ignore_id is not None:
            qs = qs.exclude(id=ignore_id)
        if not qs.exists():
            return candidate
        i += 1
        candidate = f"{root}-{i}"


# ── Catalog (read-only) ─────────────────────────────────────────────────────


class ShapeCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """`GET /api/builder/catalog/categories/` — palette grouped by category."""

    queryset = ShapeCategory.objects.filter(is_active=True).prefetch_related("shapes")
    serializer_class = ShapeCategorySerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "slug"


class ShapeDefinitionViewSet(viewsets.ReadOnlyModelViewSet):
    """`GET /api/builder/catalog/shapes/` — flat list of every palette item."""

    queryset = ShapeDefinition.objects.filter(is_active=True)
    serializer_class = ShapeDefinitionSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "slug"

    def get_queryset(self):
        qs = super().get_queryset()
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category__slug=category)
        return qs


# ── Server-driven chrome ────────────────────────────────────────────────────


class NavItemViewSet(viewsets.ReadOnlyModelViewSet):
    """`GET /api/builder/ui/navigation/` — sidebar entries the user can see."""

    serializer_class = NavItemSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = NavItem.objects.filter(is_active=True)
        # Hide ADMIN-only items from MEMBER tokens.
        role = getattr(self.request.user, "role", "MEMBER")
        if role != "ADMIN":
            qs = qs.exclude(min_role="ADMIN")
        return qs


class DashboardWidgetViewSet(viewsets.ReadOnlyModelViewSet):
    """`GET /api/builder/ui/dashboard/` — dashboard tiles."""

    queryset = DashboardWidget.objects.filter(is_active=True)
    serializer_class = DashboardWidgetSerializer
    permission_classes = [IsAuthenticated]


# ── Workflows ───────────────────────────────────────────────────────────────


class WorkflowViewSet(viewsets.ModelViewSet):
    """CRUD + duplicate / activate / deactivate / graph."""

    queryset = Workflow.objects.all()
    serializer_class = WorkflowSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        is_active = self.request.query_params.get("is_active")
        if is_active in {"true", "false"}:
            qs = qs.filter(is_active=(is_active == "true"))
        return qs.order_by("-updated_at")

    # ── Create / Update — auto-slug ─────────────────────────────────────────

    def perform_create(self, serializer):
        user = self.request.user
        # The serializer declares ``sop_urls`` and ``runtime_agents`` as
        # write-only, so pop them off validated_data before saving the row.
        validated = serializer.validated_data
        sop_urls       = validated.pop("sop_urls", None)
        runtime_agents = validated.pop("runtime_agents", None)
        auto_build     = validated.pop("auto_build_from_sop", True)

        workflow = serializer.save(
            slug=_unique_workflow_slug(validated["name"]),
            owner_id=getattr(user, "id", ""),
            owner_email=getattr(user, "email", ""),
        )

        # Opt-in add-on: flag the workflow so the post-ingestion hook auto-
        # builds the canvas from the ingested SOP(s). Stored before dispatch so
        # the ingestion subprocess sees it. No-op for the existing flow.
        if auto_build and sop_urls:
            meta = dict(workflow.metadata or {})
            meta["auto_build_canvas"] = True
            meta["source_sop"] = sop_urls[0]
            workflow.metadata = meta
            workflow.save(update_fields=["metadata", "updated_at"])

        if sop_urls or runtime_agents:
            attach_to_workflow(
                workflow,
                sop_urls=sop_urls or [],
                runtime_agents=runtime_agents or [],
            )

    # ── /graph — atomic bulk read / write ───────────────────────────────────

    @action(detail=True, methods=["get", "put"], url_path="graph")
    def graph(self, request, pk=None):
        workflow = self.get_object()
        if request.method == "PUT":
            WorkflowGraphWriter(workflow).save(request.data)
        # Reload with the nested graph prefetched (shape definitions + rule/tool
        # bindings) so serialization is a handful of queries instead of an N+1
        # per shape, and precompute the workflow-wide out-of-scope rule set once.
        workflow = self._graph_prefetched(workflow.pk)
        all_shapes = [
            sh
            for wa in workflow.work_areas.all()
            for wb in wa.workbenches.all()
            for sh in wb.shapes.all()
        ]
        context = self.get_serializer_context()
        try:
            from .bindings_sync import out_of_scope_keys_for_shapes
            context["oos_keys"] = out_of_scope_keys_for_shapes(all_shapes)
        except Exception:
            context["oos_keys"] = None
        return Response(WorkflowGraphSerializer(workflow, context=context).data)

    @staticmethod
    def _graph_prefetched(pk):
        """Fetch a workflow with its full nested graph prefetched for read.

        select_related on each shape's definition + Prefetch of the rule/tool
        bindings (with their own select_related) collapses the per-shape
        definition/binding queries into a fixed, small number.
        """
        from django.db.models import Prefetch

        shapes_qs = Shape.objects.select_related("definition").order_by("order")
        try:
            from agent_tools.models import NodeRuleBinding, NodeToolBinding
            shapes_qs = shapes_qs.prefetch_related(
                Prefetch("rule_bindings",
                         queryset=NodeRuleBinding.objects.order_by("ordering")),
                Prefetch("tool_bindings",
                         queryset=NodeToolBinding.objects
                         .select_related("tool", "rule_binding")
                         .order_by("ordering")),
            )
        except Exception:
            pass
        return (
            Workflow.objects
            .prefetch_related(
                Prefetch("work_areas__workbenches__shapes", queryset=shapes_qs),
            )
            .get(pk=pk)
        )

    # ── Auto-build progress (polled by the SPA loading screen) ──────────────

    @action(detail=True, methods=["get"], url_path="build_status")
    def build_status(self, _request, pk=None):
        """Real-time progress for an auto-built-from-SOP workflow.

        The SPA shows a loading screen (with streaming stage logs) instead of
        the empty canvas until ``phase == "done"``.  No-op for regular
        workflows (``auto_build`` is False → SPA renders the canvas directly).

        Phases: ``queued`` → ``ingesting`` → ``building`` → ``done`` (or
        ``failed``).  ``idle`` means this workflow was not flagged for
        auto-build.
        """
        wf = self.get_object()
        meta = wf.metadata or {}
        auto_build = bool(meta.get("auto_build_canvas"))
        built = bool(meta.get("auto_build_complete"))

        jobs = list(wf.ingestion_jobs.all().order_by("created_at"))
        statuses = [j.status for j in jobs]
        shape_count = Shape.objects.filter(
            workbench__work_area__workflow=wf
        ).count()

        if not auto_build:
            phase = "idle"
        elif built:
            phase = "done"
        elif jobs and all(s == "FAILED" for s in statuses):
            phase = "failed"
        elif any(s == "RUNNING" for s in statuses):
            phase = "ingesting"
        elif jobs and all(s in {"COMPLETED", "FAILED", "PARTIAL"}
                          for s in statuses):
            # Ingestion finished; the post-ingest hook is building the canvas.
            phase = "building"
        else:
            phase = "queued"

        # ── Stream the most recent stage + LLM logs ─────────────────────────
        logs: list[dict] = []
        llm_errors: list[dict] = []
        job_ids = [j.job_id for j in jobs]
        if job_ids:
            try:
                from sop_ingestion.models import PipelineStageLog, LLMCallLog
                stage_qs = (
                    PipelineStageLog.objects
                    .filter(job_id__in=job_ids)
                    .order_by("-started_at")[:120]
                )
                for lg in reversed(list(stage_qs)):
                    logs.append({
                        "stage": lg.stage_name,
                        "status": lg.status,
                        "doc_url": (lg.doc_url or "")[:160],
                        "duration_ms": lg.duration_ms,
                        "ts": lg.started_at.isoformat() if lg.started_at else None,
                        "error": (lg.error_detail or "")[:400],
                    })
                err_qs = (
                    LLMCallLog.objects
                    .filter(job_id__in=job_ids)
                    .exclude(error_message="")
                    .order_by("-id")[:10]
                )
                for e in reversed(list(err_qs)):
                    llm_errors.append({"error": (e.error_message or "")[:400]})
            except Exception:  # logs are best-effort; never break the poll
                pass

        return Response({
            "auto_build":   auto_build,
            "phase":        phase,
            "built":        built,
            "needs_tools":  bool(meta.get("needs_tools")),
            "shape_count":  shape_count,
            "stats":        meta.get("auto_build_stats"),
            "jobs": [
                {
                    "job_id":         str(j.job_id),
                    "seed_url":       j.seed_url,
                    "status":         j.status,
                    "docs_processed": j.docs_processed,
                    "docs_failed":    j.docs_failed,
                }
                for j in jobs
            ],
            "logs":        logs,
            "llm_errors":  llm_errors,
        })

    @action(detail=True, methods=["get"], url_path="build_stream")
    def build_stream(self, _request, pk=None):
        """Server-Sent Events stream of live pipeline stage logs for the build.

        Tails ``PipelineStageLog`` (Postgres, written in real time by the
        ingestion subprocess) and pushes each new row as it appears, so the
        SPA's build screen shows progress without client-side polling. The
        authoritative "done" gate is still ``build_status`` — this stream is
        purely for low-latency log tailing and emits a terminal event when the
        canvas is built or the job fails.

        Consume with fetch + ReadableStream (keeps the JWT header; the native
        EventSource API cannot send Authorization).
        """
        import json
        import time

        from django.db import close_old_connections
        from django.http import StreamingHttpResponse

        wf_pk = self.get_object().pk
        HEARTBEAT_S = 15
        POLL_S = 1.0
        MAX_S = 15 * 60

        def _event(kind: str, payload: dict) -> str:
            return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"

        def _stream():
            from sop_ingestion.models import PipelineStageLog, LLMCallLog

            last_log_id = 0
            last_err_id = 0
            last_beat = time.monotonic()
            started = time.monotonic()

            yield _event("open", {"ok": True})

            while True:
                close_old_connections()
                # Re-read the workflow + jobs fresh each tick.
                wf = Workflow.objects.filter(pk=wf_pk).first()
                if wf is None:
                    yield _event("error", {"detail": "workflow gone"})
                    return
                meta = wf.metadata or {}
                job_ids = list(
                    wf.ingestion_jobs.values_list("job_id", flat=True)
                )

                if job_ids:
                    new_logs = (
                        PipelineStageLog.objects
                        .filter(id__gt=last_log_id, job_id__in=job_ids)
                        .order_by("id")[:200]
                    )
                    for lg in new_logs:
                        last_log_id = lg.id
                        yield _event("log", {
                            "stage": lg.stage_name,
                            "status": lg.status,
                            "doc_url": (lg.doc_url or "")[:160],
                            "duration_ms": lg.duration_ms,
                            "ts": lg.started_at.isoformat() if lg.started_at else None,
                            "error": (lg.error_detail or "")[:400],
                        })

                    new_errs = (
                        LLMCallLog.objects
                        .filter(id__gt=last_err_id, job_id__in=job_ids)
                        .exclude(error_message="")
                        .order_by("id")[:20]
                    )
                    for e in new_errs:
                        last_err_id = e.id
                        yield _event("llm_error", {
                            "error": (e.error_message or "")[:400],
                        })

                # Terminal conditions.
                if meta.get("auto_build_complete"):
                    yield _event("done", {
                        "stats": meta.get("auto_build_stats"),
                        "needs_tools": bool(meta.get("needs_tools")),
                    })
                    return
                statuses = list(
                    wf.ingestion_jobs.values_list("status", flat=True)
                )
                if statuses and all(s == "FAILED" for s in statuses):
                    yield _event("failed", {"detail": "ingestion failed"})
                    return

                now = time.monotonic()
                if now - started > MAX_S:
                    yield _event("timeout", {})
                    return
                if now - last_beat > HEARTBEAT_S:
                    last_beat = now
                    yield ": ping\n\n"

                time.sleep(POLL_S)

        resp = StreamingHttpResponse(
            _stream(), content_type="text/event-stream"
        )
        resp["Cache-Control"] = "no-cache"
        resp["X-Accel-Buffering"] = "no"  # disable proxy buffering (nginx)
        return resp

    # ── Lifecycle helpers ───────────────────────────────────────────────────

    @action(detail=True, methods=["post"])
    def activate(self, _request, pk=None):
        wf = self.get_object()
        wf.is_active = True
        wf.save(update_fields=["is_active", "updated_at"])
        return Response(WorkflowSerializer(wf).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, _request, pk=None):
        wf = self.get_object()
        wf.is_active = False
        wf.save(update_fields=["is_active", "updated_at"])
        return Response(WorkflowSerializer(wf).data)

    @action(detail=True, methods=["get"], url_path="attachable")
    def attachable(self, _request, pk=None):
        """Enumerate everything a node on this workflow's canvas can attach to.

        Returns four lists keyed by stable string keys the SPA can store
        verbatim on ``Shape.properties``:

        * ``sop_rules`` — one entry per individual rule row in any completed
          SOP linked to this workflow.  Sources both pre-condition rules
          (``llm_rules``) and decision-tree rows.  Each rule carries:
            - ``references``   — keys of rules a goto/skip-to depends on,
            - ``excluded_by``  — keys of *exclusions* that override this rule
                                 (computed from the agentic graph's
                                 ``OVERRIDES`` edges),
            - ``graph_node_key`` — corresponding node in the knowledge graph,
            - ``html_reference`` — source URL + section anchor + raw text
                                   the SPA can render as the original
                                   document context.
        * ``exclusions`` — dedicated list of exclusion / exception rules
          (``rule_kind="exclusion"`` or ``is_exception=true`` or
          ``OVERRIDES`` edges sourced from the rule). Each exclusion exposes
          the list of ``overrides_rule_keys`` it neutralises, so the SPA
          can show "these rules are excluded" when an exclusion is picked.
        * ``sops`` — narrative summary per SOP.
        * ``tool_calls`` — registered runtime API agents.
        """
        from sop_ingestion.models import (  # local to avoid cycles
            AuditSop, AuditGraphNode, AuditGraphEdge, SopExclusion,
        )
        wf: Workflow = self.get_object()

        sop_rules: list[dict] = []
        sop_summaries: list[dict] = []
        sops_qs = AuditSop.objects.filter(job__workflow=wf).prefetch_related(
            "preconditions", "steps__decisions",
        ).order_by("id")
        # doc_format per SOP — drives whether the SPA shows an HTML iframe
        # (when the source is reachable) or a plain text panel (DOCX/PDF
        # uploads, where the snippet is the only viewable form).
        sop_meta_by_id: dict[int, dict] = {}

        # ── 1. Pull every OVERRIDES edge into a map keyed by source graph
        #       node key so we can derive (rule → excluded_by exclusions).
        sop_ids = list(sops_qs.values_list("id", flat=True))
        override_edges = (
            AuditGraphEdge.objects
            .filter(sop_id__in=sop_ids, rel_type="OVERRIDES")
            .select_related("source", "target")
        )
        # exclusion node_key  → list[target node_keys]
        overrides_by_src: dict[tuple[int, str], list[str]] = {}
        # target node_key    → list[exclusion node_keys]
        excluded_by_tgt:   dict[tuple[int, str], list[str]] = {}
        for e in override_edges:
            src_key = (e.sop_id, e.source.node_key)
            tgt_key = (e.sop_id, e.target.node_key)
            overrides_by_src.setdefault(src_key, []).append(e.target.node_key)
            excluded_by_tgt.setdefault(tgt_key, []).append(e.source.node_key)

        # ── 2. Iterate SOPs and emit rules + exclusions. Maintain two maps
        #       so we can resolve graph node_key ↔ flat rule key once both
        #       passes are done.
        rule_key_by_graph: dict[tuple[int, str], str] = {}
        rules_buffer: list[dict] = []      # filled with graph_node_key set
        exclusions: list[dict] = []

        for sop in sops_qs:
            sop_title = sop.title or f"SOP #{sop.id}"
            sop_url   = sop.url or ""
            doc_format = sop.doc_format or "HTML"
            sop_meta_by_id[sop.id] = {
                "title": sop_title, "url": sop_url, "doc_format": doc_format,
            }
            sop_summaries.append({
                "sop_id":     sop.id,
                "title":      sop_title,
                "narrative":  sop.narrative_context or sop.llm_summary or "",
                "source_url": sop_url,
                "doc_format": doc_format,
            })

            # Pre-index step → decision-row keys (used by goto_step refs)
            step_to_keys: dict[int, list[str]] = {}
            for step in sop.steps.all().order_by("step_number"):
                step_to_keys[step.step_number] = [
                    f"step:{sop.id}:{step.step_number}:{d.row_index}"
                    for d in step.decisions.all().order_by("row_index")
                ]

            # Index this SOP's PRE_RULE/PRE_SECTION graph nodes by display_order
            # so we can attach graph node_keys to AuditPrecondition.llm_rules.
            pre_rule_nodes = {}   # (display_order_1based, rule_idx_1based) → node_key
            for n in AuditGraphNode.objects.filter(
                sop_id=sop.id, node_type="PRE_RULE",
            ):
                m = re.match(r"^pre_(\d+)_r(\d+)$", n.node_key or "")
                if m:
                    pre_rule_nodes[(int(m.group(1)), int(m.group(2)))] = n.node_key

            # Pre-condition rules
            for pc in sop.preconditions.all().order_by("display_order", "id"):
                rules = pc.llm_rules or []
                for idx, r in enumerate(rules):
                    cond   = (r.get("condition") or "").strip()
                    action = (r.get("action") or "").strip()
                    dtype  = (r.get("decision_type") or "").strip()
                    rule_kind = (r.get("rule_kind") or "").strip().lower()
                    is_excl = (
                        bool(r.get("is_exception"))
                        or rule_kind in {"exclusion", "exception"}
                        or (pc.category or "").upper() == "EXCLUSION"
                    )

                    rule_key  = f"pre:{sop.id}:{pc.id}:{idx}"
                    graph_key = pre_rule_nodes.get(
                        (pc.display_order + 1, idx + 1), ""
                    )
                    if graph_key:
                        rule_key_by_graph[(sop.id, graph_key)] = rule_key

                    html_ref = {
                        "source_url":   sop_url,
                        "doc_format":   doc_format,
                        "anchor":       graph_key or f"pre_{pc.display_order + 1}",
                        "section_label": pc.label or pc.category,
                        "snippet_text": pc.content_text or "",
                        "snippet_html": _text_to_basic_html(pc.content_text or ""),
                    }

                    entry = {
                        "key":             rule_key,
                        "sop_id":          sop.id,
                        "sop_title":       sop_title,
                        "source":          "precondition",
                        "section_id":      pc.id,
                        "section_label":   pc.label or pc.category,
                        "section_category": pc.category,
                        "section_narrative": pc.content_text or "",
                        "condition":       cond,
                        "action":          action,
                        "decision_type":   dtype,
                        "is_exception":    is_excl,
                        "is_exclusion":    is_excl,
                        "rule_kind":       rule_kind or ("exclusion" if is_excl else "rule"),
                        "codes":           [],
                        "is_blocking":     pc.is_blocking,
                        "references":     [],
                        "goto_step":       None,
                        "graph_node_key":  graph_key,
                        "excluded_by":     [],   # filled in second pass
                        "html_reference":  html_ref,
                    }
                    rules_buffer.append(entry)

                    if is_excl:
                        # Exclusions also get their own dedicated entry — the
                        # SPA shows these as a separate selector group.
                        exclusions.append({
                            "key":             rule_key,
                            "sop_id":          sop.id,
                            "sop_title":       sop_title,
                            "section_label":   pc.label or pc.category,
                            "category":        pc.category,
                            "label":           cond or action[:120] or pc.label,
                            "condition":       cond,
                            "action":          action,
                            "decision_type":   dtype,
                            "rule_kind":       entry["rule_kind"],
                            "graph_node_key":  graph_key,
                            "overrides_rule_keys": [],   # filled in second pass
                            "html_reference":  html_ref,
                        })

            # Decision rules
            for step in sop.steps.all().order_by("step_number"):
                section_label = (
                    f"Step {step.step_number}"
                    + (f": {step.question}" if step.question else "")
                )
                for d in step.decisions.all().order_by("row_index"):
                    codes = list(d.all_codes or []) or [
                        *(d.eob_codes or []),
                        *(d.ex_codes or []),
                        *(d.denial_codes or []),
                        *(d.system_actions or []),
                    ]
                    cond_parts = [p for p in [d.condition_if, d.condition_and] if p]
                    # Row-level routing: persisted goto_step, else recover an
                    # explicit target from any of the row's text fields.
                    row_goto = d.goto_step
                    if row_goto is None:
                        row_goto = extract_goto(" ".join(filter(None, [
                            d.action_text, d.action_summary, d.condition_if,
                            d.condition_and, d.output_text,
                        ])))
                    # Step-level routing carried only in the step narrative
                    # (deny branch's "proceed to step 9" left in intro_text).
                    step_gotos = extract_all_gotos(
                        " ".join(filter(None, [step.intro_text,
                                               step.narrative_context,
                                               step.question]))
                    )
                    ref_steps: list[int] = []
                    for n in ([row_goto] if row_goto is not None else []) + step_gotos:
                        if n in step_to_keys and n not in ref_steps:
                            ref_steps.append(n)
                    references: list[str] = []
                    for n in ref_steps:
                        references.extend(step_to_keys[n])

                    rule_key  = f"step:{sop.id}:{step.step_number}:{d.row_index}"
                    graph_key = f"step_{step.step_number}_d{d.row_index}"
                    rule_key_by_graph[(sop.id, graph_key)] = rule_key

                    html_ref = {
                        "source_url":    sop_url,
                        "doc_format":    doc_format,
                        "anchor":        graph_key,
                        "section_label": section_label,
                        "snippet_text":  (step.intro_text or "")
                            + ("\n\n" if (step.intro_text and (d.action_text or "")) else "")
                            + (d.action_text or d.action_summary or ""),
                        "snippet_html":  _text_to_basic_html(
                            (step.intro_text or "")
                            + "\n\n" + (d.action_text or d.action_summary or "")
                        ),
                    }

                    rules_buffer.append({
                        "key":             rule_key,
                        "sop_id":          sop.id,
                        "sop_title":       sop_title,
                        "source":          "decision",
                        "section_id":      step.step_number,
                        "section_label":   section_label,
                        "section_category": "DECISION",
                        "subrule_id":      d.subrule_id or "",
                        "depth":           d.depth,
                        "is_out_of_scope": d.is_out_of_scope,
                        "section_narrative": step.narrative_context or step.intro_text or "",
                        "condition":       " AND ".join(cond_parts),
                        "action":          d.action_text or d.action_summary or "",
                        "decision_type":   d.decision_type or "",
                        "is_exception":    False,
                        "is_exclusion":    False,
                        "rule_kind":       "decision",
                        "codes":           codes,
                        "is_blocking":     d.is_final,
                        "references":      references,
                        "goto_step":       row_goto,
                        "step_goto_step":  (step_gotos[0] if step_gotos else None),
                        "step_goto_steps": step_gotos,
                        "graph_node_key":  graph_key,
                        "excluded_by":     [],
                        "html_reference":  html_ref,
                    })

        # ── 3. Second pass: now that every rule has a graph_node_key, resolve
        #       OVERRIDES edges in both directions.
        # Build a step-level fan-out: an exclusion that overrides "step_N"
        # implicitly excludes EVERY decision row inside step N.
        step_to_rule_keys: dict[tuple[int, str], list[str]] = {}
        for (sop_id, gkey), rkey in rule_key_by_graph.items():
            m = re.match(r"^step_(\d+)$", gkey or "")
            # ignore — handled below from the rules themselves
        for r in rules_buffer:
            if r["source"] == "decision":
                gk = f"step_{r['section_id']}"
                step_to_rule_keys.setdefault((r["sop_id"], gk), []).append(r["key"])

        def _resolve_targets(sop_id: int, target_node_keys: list[str]) -> list[str]:
            out: list[str] = []
            for tk in target_node_keys:
                # Direct decision/precondition target
                rk = rule_key_by_graph.get((sop_id, tk))
                if rk:
                    out.append(rk)
                    continue
                # STEP-level override → fan out to every decision row in step
                fan = step_to_rule_keys.get((sop_id, tk), [])
                out.extend(fan)
            return list(dict.fromkeys(out))  # dedupe, preserve order

        # Fill excluded_by on every rule
        for r in rules_buffer:
            gk = r.get("graph_node_key")
            if not gk:
                continue
            excl_node_keys = excluded_by_tgt.get((r["sop_id"], gk), [])
            # Also include exclusions that target the parent STEP, if this
            # rule is a decision row.
            if r["source"] == "decision":
                step_gk = f"step_{r['section_id']}"
                excl_node_keys = excl_node_keys + excluded_by_tgt.get(
                    (r["sop_id"], step_gk), []
                )
            if not excl_node_keys:
                continue
            r["excluded_by"] = [
                rule_key_by_graph[(r["sop_id"], nk)]
                for nk in dict.fromkeys(excl_node_keys)
                if (r["sop_id"], nk) in rule_key_by_graph
            ]

        # Fill overrides_rule_keys on every exclusion
        for ex in exclusions:
            gk = ex.get("graph_node_key")
            if not gk:
                continue
            target_node_keys = overrides_by_src.get((ex["sop_id"], gk), [])
            ex["overrides_rule_keys"] = _resolve_targets(
                ex["sop_id"], target_node_keys
            )

        # ── 4. Merge USER-curated SopExclusion rows ─────────────────────────
        # These are exclusions an auditor picked in the UI (not LLM-derived).
        # We surface them in the same `exclusions[]` list (source="user") and
        # also propagate them onto each affected rule's `excluded_by`.
        user_rules_by_key = {r["key"]: r for r in rules_buffer}
        # rule_key by graph_node_key for graph_node-kind exclusions
        rule_keys_by_step: dict[tuple[int, int], list[str]] = {}
        for r in rules_buffer:
            if r["source"] == "decision":
                rule_keys_by_step.setdefault(
                    (r["sop_id"], r["section_id"]), []
                ).append(r["key"])
        # rule_keys by precondition section id for "section" kind
        rule_keys_by_section: dict[tuple[int, int], list[str]] = {}
        for r in rules_buffer:
            if r["source"] == "precondition":
                rule_keys_by_section.setdefault(
                    (r["sop_id"], r["section_id"]), []
                ).append(r["key"])

        def _user_exclusion_targets(ex: SopExclusion) -> list[str]:
            """Resolve a SopExclusion to the list of rule_keys it affects."""
            kind = ex.target_kind
            key  = ex.target_key or ""
            if kind == "rule":
                return [key] if key in user_rules_by_key else []
            if kind == "step":
                m = re.match(r"^step:(\d+):(\d+)$", key)
                if not m:
                    return []
                return rule_keys_by_step.get(
                    (int(m.group(1)), int(m.group(2))), []
                )
            if kind == "section":
                m = re.match(r"^pre:(\d+):(\d+)$", key)
                if not m:
                    return []
                return rule_keys_by_section.get(
                    (int(m.group(1)), int(m.group(2))), []
                )
            if kind == "sop":
                m = re.match(r"^sop:(\d+)$", key)
                if not m:
                    return []
                sid = int(m.group(1))
                return [r["key"] for r in rules_buffer if r["sop_id"] == sid]
            if kind == "graph_node":
                rk = rule_key_by_graph.get((ex.sop_id, key))
                if rk:
                    return [rk]
                # graph step node → fan-out to its decision rows
                mm = re.match(r"^step_(\d+)$", key)
                if mm:
                    return rule_keys_by_step.get(
                        (ex.sop_id, int(mm.group(1))), []
                    )
                return []
            return []

        user_excl_qs = (
            SopExclusion.objects
            .filter(sop_id__in=sop_ids)
            .order_by("-updated_at")
        )
        for u in user_excl_qs:
            meta = sop_meta_by_id.get(u.sop_id, {})
            stable_key = f"user-excl:{u.id}"
            affected = _user_exclusion_targets(u)
            # html_block snippets ARE pre-rendered HTML (the extractor
            # captures the original DOM fragment); everything else stores
            # plaintext that we render into basic HTML here.
            md = u.metadata or {}
            snippet = u.snippet_text or ""
            snippet_html = (
                snippet
                if u.target_kind == "html_block" or md.get("snippet_is_html")
                else _text_to_basic_html(snippet)
            )
            html_ref = {
                "source_url":    meta.get("url", ""),
                "doc_format":    meta.get("doc_format", "HTML"),
                "anchor":        u.target_key,
                "section_label": md.get("section_label", u.label or u.target_key),
                "snippet_text":  snippet,
                "snippet_html":  snippet_html,
            }
            exclusions.append({
                "key":            stable_key,
                "id":             u.id,
                "sop_id":         u.sop_id,
                "sop_title":      meta.get("title", f"SOP #{u.sop_id}"),
                "source":         "user",
                "target_kind":    u.target_kind,
                "target_key":     u.target_key,
                "section_label":  html_ref["section_label"],
                "category":       (u.metadata or {}).get("category", "USER"),
                "label":          u.label or u.target_key,
                "reason":         u.reason or "",
                "condition":      u.label or u.target_key,
                "action":         "(excluded by auditor)",
                "decision_type":  "EXCLUSION",
                "rule_kind":      "exclusion",
                "graph_node_key": (u.metadata or {}).get("graph_node_key", ""),
                "overrides_rule_keys": affected,
                "created_by":     u.created_by_email or u.created_by_id or "",
                "created_at":     u.created_at.isoformat() if u.created_at else None,
                "html_reference": html_ref,
            })
            # Propagate to affected rules' excluded_by
            for rk in affected:
                row = user_rules_by_key.get(rk)
                if row is not None and stable_key not in row["excluded_by"]:
                    row["excluded_by"].append(stable_key)

        sop_rules = rules_buffer

        # ── tool_calls — now sourced from the agent_tools.Tool table so
        #    every LangChain tool AND every registered runtime agent shows
        #    up in the rule-attach modal / left palette / config panel
        #    under a single ``tool_kind`` discriminator.
        tool_calls: list[dict] = []
        try:
            from agent_tools.models import Tool as _Tool
            for t in _Tool.objects.filter(is_active=True).order_by("display_name"):
                tool_calls.append({
                    "key":           f"tool:{t.name}",
                    "tool_id":       str(t.id),
                    "name":          t.name,
                    "display_name":  t.display_name,
                    "description":   t.description,
                    "tool_kind":     t.kind,
                    "kind":          t.kind,
                    "invoke_url":    t.invoke_url,
                    "args_schema":   t.args_schema or {},
                    "endpoint_id":   t.endpoint_id,
                    "method":        (t.metadata or {}).get("method", "GET" if t.kind == "api_agent" else "POST"),
                    "url":           t.invoke_url,
                    "auth_type":     (t.metadata or {}).get("auth_type", "none"),
                })
        except Exception:
            # During very early bootstrap (before agent_tools is migrated)
            # fall back to the legacy metadata-only shape.
            agents = (wf.metadata or {}).get("runtime_agents") or []
            tool_calls = [{
                "key":         f"agent:{a.get('endpoint_id', '') or a.get('name', '')}",
                "tool_kind":   "api_agent",
                "endpoint_id": a.get("endpoint_id", ""),
                "name":        a.get("name", ""),
                "display_name": a.get("name", ""),
                "method":      a.get("method", "GET"),
                "url":         a.get("url", ""),
                "description": a.get("description", ""),
                "auth_type":   a.get("auth_type", "none"),
                "args_schema": {},
            } for a in agents if a.get("endpoint_id") or a.get("name")]

        return Response({
            "sops":       sop_summaries,
            "sop_rules":  sop_rules,
            "exclusions": exclusions,
            "tool_calls": tool_calls,
        })

    @action(detail=True, methods=["post"], url_path="attach")
    def attach(self, request, pk=None):
        """Attach additional SOP URLs or runtime agents to an existing workflow.

        Body: ``{ sop_urls?: string[], runtime_agents?: [{...}],
        auto_build_from_sop?: bool }``.
        Dispatches ingestion + endpoint registration via the same code path
        used on create. When ``auto_build_from_sop`` is true (or the workflow
        was already auto-built) the post-ingestion hook rebuilds the canvas
        from ALL of the workflow's SOPs, so a workflow accumulates N SOP
        columns over time.
        """
        wf = self.get_object()
        sop_urls = request.data.get("sop_urls") or []
        want_auto_build = bool(request.data.get("auto_build_from_sop"))
        already_auto_built = bool((wf.metadata or {}).get("auto_build_canvas"))
        if sop_urls and (want_auto_build or already_auto_built):
            meta = dict(wf.metadata or {})
            meta["auto_build_canvas"] = True
            meta.setdefault("source_sop", sop_urls[0])
            # A fresh ingestion is starting on an existing workflow. Clear the
            # terminal flags so build_status reports queued→ingesting→building
            # →done again and the SPA re-shows the live progress (SSE) screen
            # instead of staying "done" from the previous build.
            meta["auto_build_complete"] = False
            meta["needs_tools"] = False
            wf.metadata = meta
            wf.save(update_fields=["metadata", "updated_at"])
        result = attach_to_workflow(
            wf,
            sop_urls=sop_urls,
            runtime_agents=request.data.get("runtime_agents") or [],
        )
        return Response({
            "workflow": WorkflowSerializer(wf).data,
            "dispatched": result,
        }, status=status.HTTP_202_ACCEPTED)

    @staticmethod
    def _store_sop_upload(upload):
        """Validate + persist an uploaded SOP file to the upload dir.

        Returns ``{url, name, size}`` on success, or a DRF ``Response`` (4xx/5xx)
        describing the failure. Shared by ``sop_upload`` and ``create_from_upload``.
        """
        name = upload.name or "document.pdf"
        ext = os.path.splitext(name)[1].lower()
        if ext not in _SOP_UPLOAD_EXTENSIONS:
            return Response(
                {"detail": f"unsupported file type '{ext or '?'}'. "
                           f"Allowed: {', '.join(sorted(_SOP_UPLOAD_EXTENSIONS))}"},
                status=status.HTTP_400_BAD_REQUEST)

        max_bytes = 64 * 1024 * 1024
        if upload.size and upload.size > max_bytes:
            return Response({"detail": "file too large (max 64 MiB)"},
                            status=status.HTTP_400_BAD_REQUEST)

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "document"
        dest = _sop_upload_dir() / f"{uuid.uuid4().hex}_{safe}"
        try:
            with dest.open("wb") as fh:
                for chunk in upload.chunks():
                    fh.write(chunk)
        except OSError as exc:
            return Response({"detail": f"failed to store upload: {exc}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return {"url": f"file://{dest}", "name": name, "size": dest.stat().st_size}

    @action(detail=False, methods=["post"], url_path="sop_upload",
            parser_classes=[MultiPartParser, FormParser])
    def sop_upload(self, request):
        """Upload a local SOP document (PDF/DOCX/XLSX/HTML) for ingestion.

        Multipart form field ``file``. The file is stashed on the server and a
        ``file://`` seed URL is returned. The SPA then passes that URL inside
        ``sop_urls`` to create/attach exactly like an HTML link — the ingestion
        pipeline's local-file fetcher reads it and runs the same parse →
        enrich → auto-build flow. PDFs decompose into one node per Step/Action
        row just like HTML SOPs.

        Returns ``{ url, name, size }``.
        """
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "file (multipart) is required"},
                            status=status.HTTP_400_BAD_REQUEST)
        stored = self._store_sop_upload(upload)
        if isinstance(stored, Response):
            return stored
        return Response(stored, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="create_from_upload",
            parser_classes=[MultiPartParser, FormParser])
    def create_from_upload(self, request):
        """One-shot: upload a SOP file AND create an auto-built workflow.

        This is the seamless drag-drop path: a single multipart request stores
        the document, creates a Workflow flagged for canvas auto-build, and
        dispatches ingestion. The SPA immediately polls ``build_status`` /
        streams ``build_stream`` with the returned workflow id and lands on a
        fully-built canvas (one node per Step/Action row, nested rules hydrated).

        Multipart fields:
          ``file``  (required) — SOP document (PDF/DOCX/XLSX/HTML)
          ``name``  (optional) — workflow name; defaults to the file stem
          ``auto_build_from_sop`` (optional) — set ``false`` to ingest + link
            only (no canvas). Defaults true.

        Returns the created ``WorkflowSerializer`` payload (HTTP 201).
        """
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "file (multipart) is required"},
                            status=status.HTTP_400_BAD_REQUEST)
        stored = self._store_sop_upload(upload)
        if isinstance(stored, Response):
            return stored

        file_url = stored["url"]
        raw_name = (request.data.get("name") or "").strip()
        name = raw_name or os.path.splitext(stored["name"])[0] or "Untitled SOP"
        auto_build = str(
            request.data.get("auto_build_from_sop", "true")
        ).strip().lower() not in {"0", "false", "no", "off"}

        user = request.user
        metadata = {"source_sop": file_url}
        if auto_build:
            metadata["auto_build_canvas"] = True

        workflow = Workflow.objects.create(
            name=name,
            slug=_unique_workflow_slug(name),
            owner_id=getattr(user, "id", ""),
            owner_email=getattr(user, "email", ""),
            metadata=metadata,
        )
        attach_to_workflow(workflow, sop_urls=[file_url], runtime_agents=[])
        return Response(WorkflowSerializer(workflow).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        source = self.get_object()
        new_name = request.data.get("name") or f"{source.name} (copy)"
        clone = Workflow.objects.create(
            name=new_name,
            slug=_unique_workflow_slug(new_name),
            description=source.description,
            is_active=False,
            metadata=deepcopy(source.metadata),
            owner_id=getattr(request.user, "id", source.owner_id),
            owner_email=getattr(request.user, "email", source.owner_email),
        )
        shape_remap: dict[str, str] = {}
        for area in source.work_areas.all().order_by("order"):
            new_area = area.__class__.objects.create(
                workflow=clone,
                name=area.name, description=area.description, order=area.order,
                color=area.color,
                position_x=area.position_x, position_y=area.position_y,
                width=area.width, height=area.height,
                metadata=deepcopy(area.metadata),
            )
            for wb in area.workbenches.all().order_by("order"):
                new_wb = wb.__class__.objects.create(
                    work_area=new_area,
                    name=wb.name, description=wb.description,
                    node_key=wb.node_key, kind=wb.kind,
                    config=deepcopy(wb.config), order=wb.order,
                    position_x=wb.position_x, position_y=wb.position_y,
                    width=wb.width, height=wb.height,
                    style=deepcopy(wb.style),
                )
                for shape in wb.shapes.all().order_by("order"):
                    new_shape = shape.__class__.objects.create(
                        workbench=new_wb, definition=shape.definition,
                        label=shape.label, description=shape.description,
                        position_x=shape.position_x, position_y=shape.position_y,
                        width=shape.width, height=shape.height,
                        style=deepcopy(shape.style),
                        properties=deepcopy(shape.properties),
                        order=shape.order,
                    )
                    shape_remap[str(shape.id)] = str(new_shape.id)

        from .models import ShapeConnection  # local to keep top-of-file clean
        for conn in ShapeConnection.objects.filter(
            source_shape__workbench__work_area__workflow=source,
        ):
            new_from = shape_remap.get(str(conn.source_shape_id))
            new_to = shape_remap.get(str(conn.target_shape_id))
            if not (new_from and new_to):
                continue
            ShapeConnection.objects.create(
                source_shape_id=new_from, target_shape_id=new_to,
                source_port=conn.source_port, target_port=conn.target_port,
                label=conn.label, condition_label=conn.condition_label,
                waypoints=deepcopy(conn.waypoints), style=deepcopy(conn.style),
            )

        return Response(WorkflowSerializer(clone).data, status=status.HTTP_201_CREATED)


# ── Flat inspector CRUD ─────────────────────────────────────────────────────


class WorkbenchViewSet(viewsets.ModelViewSet):
    queryset = Workbench.objects.all()
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        from .serializers import _NestedWorkbenchSerializer
        return _NestedWorkbenchSerializer


class ShapeViewSet(viewsets.ModelViewSet):
    queryset = Shape.objects.all()
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        from .serializers import _NestedShapeSerializer
        return _NestedShapeSerializer

    def _sync_bindings(self, instance):
        try:
            from .bindings_sync import extract_bindings_from_properties
            extract_bindings_from_properties(instance)
        except Exception:
            pass

    def perform_create(self, serializer):
        instance = serializer.save()
        self._sync_bindings(instance)

    def perform_update(self, serializer):
        instance = serializer.save()
        self._sync_bindings(instance)
