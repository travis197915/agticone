"""
DRF serializers for the builder REST surface.

Two kinds of serializers live here:

* **Catalog** — read-only, public-by-auth, shape definitions / sidebar /
  dashboard widgets.  The whole point is that the frontend can render its
  chrome and palette purely from these payloads.

* **Domain** — workflow / work-area / workbench / shape / connection
  CRUD.  Includes a "nested" set used by `PUT /workflows/:id/graph`
  to atomically save the whole canvas in one transaction.
"""
from __future__ import annotations

from rest_framework import serializers

from .models import (
    DashboardWidget,
    NavItem,
    Shape,
    ShapeCategory,
    ShapeConnection,
    ShapeDefinition,
    WorkArea,
    Workbench,
    Workflow,
)


# ── Catalog ──────────────────────────────────────────────────────────────────


class ShapeDefinitionSerializer(serializers.ModelSerializer):
    category_slug = serializers.SlugRelatedField(
        source="category", slug_field="slug", read_only=True,
    )

    class Meta:
        model = ShapeDefinition
        fields = [
            "id", "slug", "label", "description", "kind",
            "svg_path", "viewbox",
            "default_label", "default_width", "default_height",
            "default_style", "ports", "property_schema",
            "category_slug", "order", "is_active",
        ]


class ShapeCategorySerializer(serializers.ModelSerializer):
    shapes = ShapeDefinitionSerializer(many=True, read_only=True)

    class Meta:
        model = ShapeCategory
        fields = ["id", "slug", "label", "description", "order", "is_active", "shapes"]


class NavItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = NavItem
        fields = ["id", "slug", "label", "icon", "href", "section", "min_role", "order"]


class DashboardWidgetSerializer(serializers.ModelSerializer):
    class Meta:
        model = DashboardWidget
        fields = ["id", "slug", "label", "icon", "kind", "value", "query", "color_class", "order"]


# ── Workflow (flat) ──────────────────────────────────────────────────────────


class RuntimeAgentSerializer(serializers.Serializer):
    """One API endpoint the workflow will call per-claim at execution time.

    These are write-on-create + read-on-detail.  Persisted on
    ``Workflow.metadata['runtime_agents']`` and mirrored into the
    ``api_agent_endpoints`` table via ``ApiAgentPipeline.register()``.
    """
    name        = serializers.CharField(max_length=120)
    url         = serializers.CharField(max_length=2048)
    method      = serializers.ChoiceField(
        choices=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        default="GET",
    )
    auth_type   = serializers.ChoiceField(
        choices=["none", "bearer", "api_key", "basic"],
        default="none",
    )
    auth_token  = serializers.CharField(
        required=False, allow_blank=True, write_only=True, max_length=2048,
    )
    description = serializers.CharField(required=False, allow_blank=True, max_length=500)
    # populated on response — never accepted as input
    endpoint_id = serializers.CharField(read_only=True)


class WorkflowSopStatusSerializer(serializers.Serializer):
    """Read-only view of one SOP attached to a workflow."""
    job_id         = serializers.UUIDField()
    seed_url       = serializers.CharField()
    status         = serializers.CharField()
    docs_processed = serializers.IntegerField()
    docs_failed    = serializers.IntegerField()
    created_at     = serializers.DateTimeField()
    completed_at   = serializers.DateTimeField(allow_null=True)
    # The primary AuditSop.id for this job (used to embed the graph viewer
    # in the SPA). null while the job is still queued/running.
    audit_sop_id   = serializers.SerializerMethodField()

    def get_audit_sop_id(self, obj):
        # obj is an IngestionJob row; pick the first ingested doc.
        first = obj.audit_sops.order_by("id").first()
        return first.id if first else None


class WorkflowSerializer(serializers.ModelSerializer):
    # Write-only fields accepted on create (not stored as columns).
    sop_urls = serializers.ListField(
        child=serializers.URLField(max_length=2048),
        required=False, write_only=True, allow_empty=True,
        help_text="Static-rule SOP URLs to ingest and link to this workflow.",
    )
    runtime_agents = RuntimeAgentSerializer(
        many=True, required=False, write_only=True,
        help_text="Runtime API endpoints called per-claim at execution.",
    )

    # Read-only views of attached SOPs + agents.
    sops             = serializers.SerializerMethodField(read_only=True)
    attached_agents  = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Workflow
        fields = [
            "id", "name", "slug", "description", "is_active",
            "metadata", "owner_id", "owner_email",
            "created_at", "updated_at",
            # write-only
            "sop_urls", "runtime_agents",
            # read-only
            "sops", "attached_agents",
        ]
        read_only_fields = ["id", "slug", "owner_id", "owner_email",
                            "created_at", "updated_at"]

    def get_sops(self, obj: Workflow):
        jobs = obj.ingestion_jobs.all().order_by("-created_at")
        return WorkflowSopStatusSerializer(jobs, many=True).data

    def get_attached_agents(self, obj: Workflow):
        # Source of truth is metadata.runtime_agents (written at create-time
        # by the view).  endpoint_id is included; auth_token is never echoed.
        agents = (obj.metadata or {}).get("runtime_agents") or []
        return [
            {
                "name":        a.get("name", ""),
                "url":         a.get("url", ""),
                "method":      a.get("method", "GET"),
                "auth_type":   a.get("auth_type", "none"),
                "description": a.get("description", ""),
                "endpoint_id": a.get("endpoint_id", ""),
            }
            for a in agents
        ]


# ── Graph (nested) — drives PUT /workflows/:id/graph ─────────────────────────


class _NestedShapeSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    client_id = serializers.CharField(required=False, write_only=True, allow_null=True)
    definition_slug = serializers.SlugRelatedField(
        source="definition", slug_field="slug",
        queryset=ShapeDefinition.objects.all(),
    )

    class Meta:
        model = Shape
        fields = [
            "id", "client_id", "definition_slug",
            "label", "description",
            "position_x", "position_y", "width", "height",
            "style", "properties", "order",
        ]


class _NestedWorkbenchSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    client_id = serializers.CharField(required=False, write_only=True, allow_null=True)
    shapes = _NestedShapeSerializer(many=True, required=False)

    class Meta:
        model = Workbench
        fields = [
            "id", "client_id",
            "name", "description", "node_key", "kind", "config", "order",
            "position_x", "position_y", "width", "height", "style",
            "shapes",
        ]


class _NestedWorkAreaSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    client_id = serializers.CharField(required=False, write_only=True, allow_null=True)
    workbenches = _NestedWorkbenchSerializer(many=True, required=False)

    class Meta:
        model = WorkArea
        fields = [
            "id", "client_id",
            "name", "description", "order", "color",
            "position_x", "position_y", "width", "height",
            "metadata", "workbenches",
        ]


class _NestedConnectionSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    # Endpoint refs — client may supply any one of:
    #   *_shape (UUID)   — existing or freshly-created shape id
    #   *_client_id      — a client-side temp id that appears in this payload
    source_shape = serializers.CharField(required=False, allow_null=True, source="source_shape_id")
    target_shape = serializers.CharField(required=False, allow_null=True, source="target_shape_id")
    source_client_id = serializers.CharField(required=False, allow_null=True, write_only=True)
    target_client_id = serializers.CharField(required=False, allow_null=True, write_only=True)

    class Meta:
        model = ShapeConnection
        fields = [
            "id",
            "source_shape", "target_shape",
            "source_client_id", "target_client_id",
            "source_port", "target_port",
            "label", "condition_label", "waypoints", "style",
        ]


class WorkflowGraphSerializer(serializers.ModelSerializer):
    """
    What `GET /workflows/:id/graph` returns and what `PUT /…/graph` accepts.
    """

    work_areas      = _NestedWorkAreaSerializer(many=True, required=False)
    connections     = serializers.SerializerMethodField()
    sops            = serializers.SerializerMethodField(read_only=True)
    attached_agents = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Workflow
        fields = [
            "id", "name", "slug", "description", "is_active",
            "metadata", "created_at", "updated_at",
            "work_areas", "connections",
            "sops", "attached_agents",
        ]

    def get_connections(self, obj: Workflow):
        qs = ShapeConnection.objects.filter(
            source_shape__workbench__work_area__workflow=obj,
        ).order_by("created_at")
        return _NestedConnectionSerializer(qs, many=True).data

    def get_sops(self, obj: Workflow):
        jobs = obj.ingestion_jobs.all().order_by("-created_at")
        return WorkflowSopStatusSerializer(jobs, many=True).data

    def get_attached_agents(self, obj: Workflow):
        agents = (obj.metadata or {}).get("runtime_agents") or []
        return [
            {
                "name":        a.get("name", ""),
                "url":         a.get("url", ""),
                "method":      a.get("method", "GET"),
                "auth_type":   a.get("auth_type", "none"),
                "description": a.get("description", ""),
                "endpoint_id": a.get("endpoint_id", ""),
            }
            for a in agents
        ]
