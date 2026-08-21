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
    # Bumped whenever any Workbench under this workflow gets a content version
    # bump (see builder.sop_autobuild.sync_workflow_from_job). Independent of
    # AuditSop.version_number — lets a claim run record "which configuration of
    # this workflow" it executed against.
    version = models.PositiveIntegerField(default=1)

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


class WorkbenchImmutableFieldError(ValueError):
    """Raised when code tries to change version/node_key, or one of
    config's identity sub-keys, on an existing Workbench row after creation.

    These are write-once by design (see builder.workflow_versioning): every
    legitimate content change creates a brand-new Workbench row instead, so a
    historical WorkflowVersionWorkbench snapshot can safely hold a plain FK
    to a Workbench row and trust it never changes underneath it. This
    exception is the enforcement of that invariant — see WorkbenchQuerySet.update()
    and Workbench.save() below, which are the two places a violation is caught
    (queryset bulk update and instance save respectively; neither alone would
    catch every path, which is why both exist).
    """


# Only these config sub-keys carry version/identity meaning (read by
# builder.workbench_versioning.find_matching_workbench/content_unchanged and
# by the WorkflowVersionWorkbench snapshot). Other keys — notably
# 'extra_context', edited in place via WorkbenchViewSet.context — are
# ordinary mutable data and are deliberately NOT protected.
_WORKBENCH_CONFIG_IDENTITY_KEYS = ("sop_id", "sop_title", "content_hash",
                                   "source_url", "yaml_ref")


class WorkbenchQuerySet(models.QuerySet):
    _PROTECTED_FIELDS = frozenset({"version", "node_key"})

    def update(self, **kwargs):
        touched = self._PROTECTED_FIELDS & set(kwargs)
        if touched:
            raise WorkbenchImmutableFieldError(
                f"Workbench.{', '.join(sorted(touched))} cannot be changed via "
                "a bulk update() — these fields are write-once once a row is "
                "created. Create a new Workbench row for the new content "
                "instead (see builder.workflow_versioning)."
            )
        if "config" in kwargs:
            raise WorkbenchImmutableFieldError(
                "Workbench.config cannot be changed via a bulk update() — "
                "its identity sub-keys "
                f"({', '.join(_WORKBENCH_CONFIG_IDENTITY_KEYS)}) are write-once. "
                "Update via a model instance (Workbench.save() enforces the "
                "same rule per sub-key) or create a new Workbench row."
            )
        return super().update(**kwargs)


class Workbench(_UUIDPK, _Timestamps):
    _PROTECTED_FIELDS = ("version", "node_key")

    work_area = models.ForeignKey(
        WorkArea, on_delete=models.CASCADE, related_name="workbenches",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    # Stable per-workflow handle — lets edges reference fresh shapes by key,
    # and identifies the "slot" a Workbench belongs to across content versions
    # (see builder.workbench_versioning). Old and new versions of the same slot
    # legitimately share this value, so it is intentionally not unique.
    # Write-once (see Workbench.save()/WorkbenchQuerySet.update() below).
    node_key = models.CharField(max_length=128, blank=True, default="", db_index=True)
    # Free-form classification (e.g. "Eligibility", "Adjudication") — drives
    # filtering / sub-palette decisions in the UI.
    kind = models.CharField(max_length=64, blank=True, default="")
    # Identity sub-keys (_WORKBENCH_CONFIG_IDENTITY_KEYS) are write-once (see
    # Workbench.save()/WorkbenchQuerySet.update() below); other keys (e.g.
    # 'extra_context') remain freely mutable in place.
    config = models.JSONField(default=dict, blank=True)
    order = models.PositiveIntegerField(default=0)
    position_x = models.FloatField(default=0)
    position_y = models.FloatField(default=0)
    width = models.FloatField(default=160)
    height = models.FloatField(default=80)
    style = models.JSONField(default=dict, blank=True)
    # Content version of the SOP bound to this Workbench slot. Bumped only when
    # the ingested SOP content actually changes (see
    # builder.workbench_versioning.content_unchanged); an unchanged re-ingest
    # leaves this untouched. Independent of AuditSop.version_number. A content
    # change always creates a NEW Workbench row (never bumped on the existing
    # row) — write-once (see Workbench.save()/WorkbenchQuerySet.update() below).
    version = models.PositiveIntegerField(default=1)
    # False once a new version of this slot has been appended — the row (and
    # its Shapes/NodeRuleBindings) is preserved for history/claim traceability
    # rather than deleted, but is excluded from the live canvas and execution.
    is_current = models.BooleanField(default=True, db_index=True)

    objects = WorkbenchQuerySet.as_manager()

    class Meta:
        db_table = "builder_workbench"
        ordering = ["work_area", "order"]
        indexes = [
            models.Index(fields=["work_area", "order"]),
            models.Index(fields=["kind"]),
            models.Index(fields=["node_key", "is_current"]),
        ]

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_protected = {
            f: getattr(instance, f) for f in cls._PROTECTED_FIELDS
            if f in field_names
        }
        if "config" in field_names:
            cfg = instance.config or {}
            instance._loaded_config_identity = {
                k: cfg.get(k) for k in _WORKBENCH_CONFIG_IDENTITY_KEYS
            }
        return instance

    def save(self, *args, **kwargs):
        loaded = getattr(self, "_loaded_protected", None)
        if loaded is not None:
            for field, prior_value in loaded.items():
                if getattr(self, field) != prior_value:
                    raise WorkbenchImmutableFieldError(
                        f"Workbench.{field} is write-once and cannot be "
                        f"changed after creation (pk={self.pk}). Create a "
                        "new Workbench row for the new content instead."
                    )
        loaded_config = getattr(self, "_loaded_config_identity", None)
        if loaded_config is not None:
            cfg = self.config or {}
            current = {k: cfg.get(k) for k in _WORKBENCH_CONFIG_IDENTITY_KEYS}
            if current != loaded_config:
                raise WorkbenchImmutableFieldError(
                    "Workbench.config's identity sub-keys "
                    f"({', '.join(_WORKBENCH_CONFIG_IDENTITY_KEYS)}) are "
                    f"write-once and cannot be changed after creation "
                    f"(pk={self.pk}). Create a new Workbench row for the "
                    "new content instead."
                )
        super().save(*args, **kwargs)
        self._loaded_protected = {
            f: getattr(self, f) for f in self._PROTECTED_FIELDS
        }
        cfg = self.config or {}
        self._loaded_config_identity = {
            k: cfg.get(k) for k in _WORKBENCH_CONFIG_IDENTITY_KEYS
        }


class WorkflowVersion(_UUIDPK, _Timestamps):
    """One immutable, timestamped snapshot of a Workflow's SOP composition.

    Created exclusively by builder.workflow_versioning.snapshot_workflow_version
    — never client-writable. ``version_number`` mirrors Workflow.version at the
    moment this snapshot was taken; the two stay in lockstep by construction
    (both written in the same transaction, in that one function). See
    ``slots`` (WorkflowVersionWorkbench) for the actual per-SOP composition —
    this row alone doesn't describe what was in the workflow.
    """
    workflow = models.ForeignKey(
        Workflow, on_delete=models.CASCADE, related_name="versions",
    )
    version_number = models.PositiveIntegerField()
    # Short breadcrumb for what triggered this snapshot — human-readable audit
    # context, not machine-authoritative. E.g. "initial_build",
    # "sop_sync:changed:SOP A", "rollout_approved".
    reason = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "builder_workflow_version"
        ordering = ["workflow", "version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow", "version_number"],
                name="uniq_workflow_version_number",
            ),
        ]


class WorkflowVersionWorkbench(_UUIDPK):
    """One SOP slot's frozen state within a WorkflowVersion snapshot.

    Every field here is written once, at snapshot-creation time, and is the
    AUTHORITATIVE source for historical display — never re-derived from live
    Workbench/AuditSop state when rendering history (order in particular is
    expected to diverge from the live Workbench.order over time via ordinary
    canvas reordering, which does not itself create a new WorkflowVersion).
    """
    workflow_version = models.ForeignKey(
        WorkflowVersion, on_delete=models.CASCADE, related_name="slots",
    )
    # PROTECT — a Workbench row referenced by any historical snapshot can
    # never be deleted. Combined with Workbench's write-once fields, this row
    # stays truthful forever once created.
    workbench = models.ForeignKey(
        Workbench, on_delete=models.PROTECT, related_name="version_snapshots",
    )
    node_key = models.CharField(max_length=128, blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    # Frozen copy of Workbench.version at snapshot time. Defense-in-depth
    # alongside the write-once guarantee above, and lets the version-history
    # API read this table alone without joining into Workbench.
    workbench_version = models.PositiveIntegerField()
    # Best-effort resolved AuditSop identity at snapshot time — independent
    # axis from workbench_version, never merged with it. Null when no
    # resolvable AuditSop was found (e.g. a hand-built Workbench with no
    # ingestion provenance).
    audit_sop_id = models.PositiveIntegerField(null=True, blank=True)
    sop_title = models.CharField(max_length=255, blank=True, default="")
    sop_version_number = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "builder_workflow_version_workbench"
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_version", "workbench"],
                name="uniq_wfv_workbench",
            ),
            models.UniqueConstraint(
                fields=["workflow_version", "node_key"],
                name="uniq_wfv_node_key",
            ),
        ]


class WorkflowVersionRule(_UUIDPK):
    """One rule's frozen configuration within a WorkflowVersion snapshot.

    Sibling of WorkflowVersionWorkbench (which snapshots *composition* — which
    Workbench/SOP occupies each slot) — this table snapshots *content*: every
    NodeRuleBinding override and every custom (``custom:{uuid}``) rule attached
    to any Shape in the workflow at the moment this version was created. Written
    once, at snapshot-creation time, by builder.workflow_versioning — never
    updated afterward. ``shape_id``/``workbench_id`` are plain fields, not FKs:
    Shape rows are mutated/deleted in place by WorkflowGraphWriter, so a
    historical row must survive a shape's later deletion without cascading
    (same "independent axis" reasoning as WorkflowVersionWorkbench.audit_sop_id).
    """
    workflow_version = models.ForeignKey(
        WorkflowVersion, on_delete=models.CASCADE, related_name="rules",
    )
    shape_id = models.UUIDField()
    # TextField, not CharField — mirrors Shape.label (some SOP step text runs
    # well past 255 chars; see builder.models.Shape.label's own comment).
    shape_label = models.TextField(blank=True, default="")
    workbench_id = models.UUIDField()
    node_key = models.CharField(max_length=128, blank=True, default="")
    rule_key = models.CharField(max_length=255, db_index=True)
    is_custom = models.BooleanField(default=False)
    condition = models.TextField(blank=True, default="")
    action = models.TextField(blank=True, default="")
    decision_type = models.CharField(max_length=64, blank=True, default="")
    codes = models.JSONField(default=list, blank=True)
    subrule_id = models.CharField(max_length=64, blank=True, default="")
    # Not a FK — same reasoning as WorkflowVersionWorkbench.audit_sop_id.
    sop_id = models.PositiveIntegerField(null=True, blank=True)
    # TextField, not CharField — mirrors AuditSop.title (also unbounded).
    sop_title = models.TextField(blank=True, default="")
    sop_version_number = models.PositiveIntegerField(null=True, blank=True)
    references_json = models.JSONField(default=list, blank=True)
    excluded_by_json = models.JSONField(default=list, blank=True)
    html_reference_json = models.JSONField(default=dict, blank=True)
    # Provenance when this rule started life as a manually-edited SOP-derived
    # rule whose source AuditDecision was later removed by a SOP rollout — see
    # sop_ingestion.services.workflow_rollout._orphan_binding_to_custom_rule.
    orphaned_from_rule_key = models.CharField(max_length=255, blank=True, default="")
    orphaned_from_sop_id = models.PositiveIntegerField(null=True, blank=True)
    orphaned_reason = models.CharField(max_length=64, blank=True, default="")
    ordering = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "builder_workflow_version_rule"
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_version", "shape_id", "rule_key"],
                name="uniq_wfv_rule",
            ),
        ]


class WorkflowVersionTool(_UUIDPK):
    """One tool binding's frozen configuration within a WorkflowVersion snapshot.

    Sibling of WorkflowVersionRule (rules) — this table snapshots the *tool*
    axis so a WorkflowVersion is the complete executable configuration, not
    rules-only. Written once, at snapshot-creation time, by
    builder.workflow_versioning — never updated afterward.

    ``shape_id``/``workbench_id`` are plain fields, not FKs — same
    survives-shape-deletion reasoning as WorkflowVersionRule. ``tool_id`` is
    also plain, not a FK — same cross-app reasoning as
    WorkflowVersionWorkbench.audit_sop_id (agent_tools.Tool lives in a
    different app; this table never imports agent_tools.models at class
    definition time).
    """
    workflow_version = models.ForeignKey(
        WorkflowVersion, on_delete=models.CASCADE, related_name="tool_bindings",
    )
    shape_id = models.UUIDField()
    workbench_id = models.UUIDField()
    node_key = models.CharField(max_length=128, blank=True, default="")
    # Not a FK — see class docstring.
    tool_id = models.UUIDField(null=True, blank=True)
    tool_name = models.CharField(max_length=255, blank=True, default="")
    # The rule this tool binding is scoped to, as a stable rule_key string
    # (not the NodeToolBinding.rule_binding FK) — same non-FK,
    # survives-mutation reasoning as WorkflowVersionRule.rule_key. Blank when
    # the tool binding isn't scoped to a specific rule (tools_by_shape).
    rule_key = models.CharField(max_length=255, blank=True, default="")
    args_template = models.JSONField(default=dict, blank=True)
    ordering = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "builder_workflow_version_tool"


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
