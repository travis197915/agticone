"""
REST views for the builder.

Exposes:
  • catalog (shape categories + shapes), sidebar nav, dashboard widgets
  • workflow CRUD + duplicate / activate / deactivate
  • atomic graph bulk save / load
  • flat inspector CRUD for workbenches + shapes
"""
from __future__ import annotations

import re
import unicodedata
from copy import deepcopy

from django.shortcuts import get_object_or_404
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
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

        workflow = serializer.save(
            slug=_unique_workflow_slug(validated["name"]),
            owner_id=getattr(user, "id", ""),
            owner_email=getattr(user, "email", ""),
        )

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
            workflow.refresh_from_db()
        return Response(WorkflowGraphSerializer(workflow).data)

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

        Returns two lists keyed by stable string keys the SPA can store
        verbatim on ``Shape.properties``:

        * ``sop_rules`` — one entry per individual rule row in any completed
          SOP linked to this workflow.  Sources both pre-condition rules
          (``llm_rules``) and decision-tree rows.  Each rule carries a
          ``references`` array listing keys of *other* rules that pick is
          dependent on — typically the destination of a ``goto_step`` /
          "Skip to <step>".  The SPA uses this to cascade-select dependent
          rules when the user picks a rule with a downstream step.
        * ``tool_calls`` — one entry per registered runtime API agent
          (from ``Workflow.metadata.runtime_agents``).
        """
        from sop_ingestion.models import AuditSop  # local to avoid cycles
        wf: Workflow = self.get_object()

        sop_rules: list[dict] = []
        sop_summaries: list[dict] = []
        sops_qs = AuditSop.objects.filter(job__workflow=wf).prefetch_related(
            "preconditions", "steps__decisions",
        ).order_by("id")

        for sop in sops_qs:
            sop_title = sop.title or f"SOP #{sop.id}"
            sop_summaries.append({
                "sop_id":    sop.id,
                "title":     sop_title,
                "narrative": sop.narrative_context or sop.llm_summary or "",
            })

            # Two-pass: pre-index step → list of decision-row keys so we can
            # resolve goto_step references into the keys of all sibling rows
            # in the target step.
            step_to_keys: dict[int, list[str]] = {}
            for step in sop.steps.all().order_by("step_number"):
                step_to_keys[step.step_number] = [
                    f"step:{sop.id}:{step.step_number}:{d.row_index}"
                    for d in step.decisions.all().order_by("row_index")
                ]

            for pc in sop.preconditions.all().order_by("display_order", "id"):
                rules = pc.llm_rules or []
                for idx, r in enumerate(rules):
                    cond   = (r.get("condition") or "").strip()
                    action = (r.get("action") or "").strip()
                    dtype  = (r.get("decision_type") or "").strip()
                    sop_rules.append({
                        "key":           f"pre:{sop.id}:{pc.id}:{idx}",
                        "sop_id":        sop.id,
                        "sop_title":     sop_title,
                        "source":        "precondition",
                        "section_id":    pc.id,
                        "section_label": pc.label or pc.category,
                        "section_category": pc.category,
                        "section_narrative": pc.content_text or "",
                        "condition":     cond,
                        "action":        action,
                        "decision_type": dtype,
                        "is_exception":  bool(r.get("is_exception")),
                        "codes":         [],
                        "is_blocking":   pc.is_blocking,
                        "references":    [],
                        "goto_step":     None,
                    })

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
                    references: list[str] = []
                    if d.goto_step is not None and d.goto_step in step_to_keys:
                        references = step_to_keys[d.goto_step]
                    sop_rules.append({
                        "key":           f"step:{sop.id}:{step.step_number}:{d.row_index}",
                        "sop_id":        sop.id,
                        "sop_title":     sop_title,
                        "source":        "decision",
                        "section_id":    step.step_number,
                        "section_label": section_label,
                        "section_category": "DECISION",
                        "section_narrative": step.narrative_context or step.intro_text or "",
                        "condition":     " AND ".join(cond_parts),
                        "action":        d.action_text or d.action_summary or "",
                        "decision_type": d.decision_type or "",
                        "is_exception":  False,
                        "codes":         codes,
                        "is_blocking":   d.is_final,
                        "references":    references,
                        "goto_step":     d.goto_step,
                    })

        agents = (wf.metadata or {}).get("runtime_agents") or []
        tool_calls = [{
            "key":         f"agent:{a.get('endpoint_id', '') or a.get('name', '')}",
            "endpoint_id": a.get("endpoint_id", ""),
            "name":        a.get("name", ""),
            "method":      a.get("method", "GET"),
            "url":         a.get("url", ""),
            "description": a.get("description", ""),
            "auth_type":   a.get("auth_type", "none"),
        } for a in agents if a.get("endpoint_id") or a.get("name")]

        return Response({
            "sops":       sop_summaries,
            "sop_rules":  sop_rules,
            "tool_calls": tool_calls,
        })

    @action(detail=True, methods=["post"], url_path="attach")
    def attach(self, request, pk=None):
        """Attach additional SOP URLs or runtime agents to an existing workflow.

        Body: ``{ sop_urls?: string[], runtime_agents?: [{...}] }``.
        Dispatches ingestion + endpoint registration via the same code path
        used on create.
        """
        wf = self.get_object()
        result = attach_to_workflow(
            wf,
            sop_urls=request.data.get("sop_urls") or [],
            runtime_agents=request.data.get("runtime_agents") or [],
        )
        return Response({
            "workflow": WorkflowSerializer(wf).data,
            "dispatched": result,
        }, status=status.HTTP_202_ACCEPTED)

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
