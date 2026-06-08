"""
builder/models.py
=================
Tables that back the drag-and-drop workflow builder.  All editor state and
every server-driven UI element lives here.

Hierarchy
---------
Workflow
  └── WorkArea            (a swim-lane / flow-chart canvas)
        └── Workbench     (a labelled flow-chart container)
              └── Shape   (one node on the canvas)
ShapeConnection           (directed edge between two Shapes —
                           may cross workbench / workarea boundaries)

Catalog (server-driven UI)
--------------------------
ShapeCategory             palette section ("General", "Flowchart" …)
ShapeDefinition           one draggable palette item
NavItem                   sidebar entry
DashboardWidget           dashboard tile
"""
from __future__ import annotations

import uuid

from django.db import models


# ── Mixins ────────────────────────────────────────────────────────────────────


class _UUIDPK(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class _Timestamps(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ── Domain ────────────────────────────────────────────────────────────────────


class Workflow(_UUIDPK, _Timestamps):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    # Free-form bag for client-side state we don't model yet.
    metadata = models.JSONField(default=dict, blank=True)
    # The user (from claims-corebackend) who created the workflow.  We
    # store the bare UUID + email rather than a FK because the auth
    # system lives in a different service.
    owner_id = models.CharField(max_length=64, blank=True, default="")
    owner_email = models.EmailField(blank=True, default="")

    class Meta:
        db_table = "builder_workflow"
        ordering = ["-updated_at"]

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return self.name


class WorkArea(_UUIDPK, _Timestamps):
    workflow = models.ForeignKey(
        Workflow, on_delete=models.CASCADE, related_name="work_areas",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    color = models.CharField(max_length=24, blank=True, default="")
    position_x = models.FloatField(default=0)
    position_y = models.FloatField(default=0)
    width = models.FloatField(default=1200)
    height = models.FloatField(default=800)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "builder_work_area"
        ordering = ["workflow", "order"]
        indexes = [models.Index(fields=["workflow", "order"])]


class Workbench(_UUIDPK, _Timestamps):
    work_area = models.ForeignKey(
        WorkArea, on_delete=models.CASCADE, related_name="workbenches",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    # Stable per-workflow handle — lets edges reference fresh shapes by key.
    node_key = models.CharField(max_length=128, blank=True, default="", db_index=True)
    # Free-form classification (e.g. "Eligibility", "Adjudication") — drives
    # filtering / sub-palette decisions in the UI.
    kind = models.CharField(max_length=64, blank=True, default="")
    config = models.JSONField(default=dict, blank=True)
    order = models.PositiveIntegerField(default=0)
    position_x = models.FloatField(default=0)
    position_y = models.FloatField(default=0)
    width = models.FloatField(default=160)
    height = models.FloatField(default=80)
    style = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "builder_workbench"
        ordering = ["work_area", "order"]
        indexes = [
            models.Index(fields=["work_area", "order"]),
            models.Index(fields=["kind"]),
        ]


class Shape(_UUIDPK, _Timestamps):
    """
    One placed flow-chart shape on the canvas.

    Each Shape references a :class:`ShapeDefinition` (the palette entry it
    was instantiated from) so the frontend can render the same SVG / ports
    without any hardcoded shape knowledge.  All instance-specific overrides
    (label text, position, custom style, free-form properties) live on the
    Shape row itself.
    """

    workbench = models.ForeignKey(
        Workbench, on_delete=models.CASCADE, related_name="shapes",
    )
    definition = models.ForeignKey(
        "ShapeDefinition", on_delete=models.PROTECT, related_name="instances",
    )
    # TextField (not CharField) so a node can carry the FULL SOP step text
    # untruncated — some audit steps run well past 255 chars.
    label = models.TextField(blank=True, default="")
    description = models.TextField(blank=True, default="")
    position_x = models.FloatField(default=0)
    position_y = models.FloatField(default=0)
    width = models.FloatField(default=120)
    height = models.FloatField(default=80)
    style = models.JSONField(default=dict, blank=True)
    # Values for the property_schema declared on the ShapeDefinition.
    properties = models.JSONField(default=dict, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "builder_shape"
        ordering = ["workbench", "order"]
        indexes = [models.Index(fields=["workbench", "order"])]


class ShapeConnection(_UUIDPK, _Timestamps):
    source_shape = models.ForeignKey(
        Shape, on_delete=models.CASCADE, related_name="outgoing",
    )
    target_shape = models.ForeignKey(
        Shape, on_delete=models.CASCADE, related_name="incoming",
    )
    source_port = models.CharField(max_length=32, blank=True, default="")
    target_port = models.CharField(max_length=32, blank=True, default="")
    label = models.CharField(max_length=255, blank=True, default="")
    condition_label = models.CharField(max_length=64, blank=True, default="")
    waypoints = models.JSONField(default=list, blank=True)
    style = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "builder_shape_connection"
        indexes = [
            models.Index(fields=["source_shape"]),
            models.Index(fields=["target_shape"]),
        ]


# ── Catalog: server-driven palette ────────────────────────────────────────────


class ShapeCategory(_UUIDPK, _Timestamps):
    slug = models.SlugField(max_length=64, unique=True)
    label = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "builder_shape_category"
        ordering = ["order", "label"]

    def __str__(self) -> str:  # pragma: no cover
        return self.label


class ShapeDefinition(_UUIDPK, _Timestamps):
    """
    A palette item.  Everything the frontend needs to render the shape and
    its inspector form is stored on this row — there is no hardcoded shape
    knowledge anywhere in the SPA.

    Fields
    ------
    `kind`               opaque identifier used by the SVG renderer
                         ('rectangle', 'ellipse', 'diamond', 'cloud' …).
                         The renderer's job is just: given a kind +
                         width/height/style, draw the path.
    `svg_path`           optional precomputed SVG `d`-attribute.  When
                         present the frontend can render the shape without
                         any kind-specific code at all.
    `viewbox`            viewBox of the svg_path (default '0 0 100 100').
    `ports`              array of `{ id, x, y, side, kind: 'source'|'target'|'both' }`
                         where x/y are 0..1 percentages of the shape.
    `default_*`          starting size + label for a freshly dropped shape.
    `default_style`      starting style (fill, stroke, etc.)
    `property_schema`    JSON-Schema-ish description that drives the inspector
                         form.  An array of `{ name, label, type, options? }`.
    """

    category = models.ForeignKey(
        ShapeCategory, on_delete=models.PROTECT, related_name="shapes",
    )
    slug = models.SlugField(max_length=64, unique=True)
    label = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    kind = models.CharField(max_length=64)
    svg_path = models.TextField(blank=True, default="")
    viewbox = models.CharField(max_length=64, default="0 0 100 100")
    default_label = models.CharField(max_length=128, blank=True, default="")
    default_width = models.FloatField(default=120)
    default_height = models.FloatField(default=80)
    default_style = models.JSONField(default=dict, blank=True)
    ports = models.JSONField(default=list, blank=True)
    property_schema = models.JSONField(default=list, blank=True)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "builder_shape_definition"
        ordering = ["category", "order", "label"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.category.label} · {self.label}"


# ── Server-driven chrome ──────────────────────────────────────────────────────


class NavItem(_UUIDPK, _Timestamps):
    """One sidebar entry.  The SPA renders the sidebar from these rows."""

    slug = models.SlugField(max_length=64, unique=True)
    label = models.CharField(max_length=128)
    icon = models.CharField(max_length=64, blank=True, default="",
                            help_text="lucide-react icon name, e.g. 'GitBranch'")
    href = models.CharField(max_length=255)
    section = models.CharField(max_length=64, blank=True, default="",
                               help_text="Sidebar group header, e.g. 'Automation'")
    # If set, only users whose role >= this value see the item.
    min_role = models.CharField(
        max_length=16,
        default="MEMBER",
        choices=[("MEMBER", "Member"), ("ADMIN", "Admin")],
    )
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "builder_nav_item"
        ordering = ["section", "order", "label"]


class DashboardWidget(_UUIDPK, _Timestamps):
    """
    Server-driven dashboard tile.  The frontend just maps `kind` to a
    visual component and reads `query` to know which endpoint to call (or
    `value` for static tiles).
    """

    slug = models.SlugField(max_length=64, unique=True)
    label = models.CharField(max_length=128)
    icon = models.CharField(max_length=64, blank=True, default="")
    # 'stat' | 'chart' | 'list' | 'card'
    kind = models.CharField(max_length=24)
    # Static value for stat tiles (display as-is); ignored when `query` is set.
    value = models.CharField(max_length=64, blank=True, default="—")
    # REST path the SPA should hit to populate the tile (relative to the
    # Django builder API base URL).
    query = models.CharField(max_length=255, blank=True, default="")
    color_class = models.CharField(max_length=64, blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "builder_dashboard_widget"
        ordering = ["order", "label"]
