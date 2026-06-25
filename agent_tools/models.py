"""
agent_tools/models.py
=====================

Three tables:

* :class:`Tool` — the registry. One row per LangChain tool ported under
  :mod:`agent_tools.tools` (auto-seeded from
  :func:`agent_tools.registry.sync_to_db`) plus one row per registered
  HTTP runtime agent (created on workflow attach).
* :class:`NodeRuleBinding` — replaces the JSON-blob ``Shape.properties.sop_rules``.
  One row = "this rule from this SOP is attached to this canvas shape".
* :class:`NodeToolBinding` — replaces ``Shape.properties.tool_calls``.
  One row = "this tool is attached to this canvas shape" with an optional
  back-pointer to the :class:`NodeRuleBinding` it was picked alongside
  (the "tools picked while attaching this rule" relationship).
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


# ── Tool registry ─────────────────────────────────────────────────────────────


class Tool(_UUIDPK, _Timestamps):
    """
    A single agent-callable tool the workflow editor can surface and attach.

    ``kind`` discriminates two flavours that live side-by-side in the same
    table:

    * ``langchain`` — a wrapped :class:`langchain_core.tools.StructuredTool`
      in :mod:`agent_tools.tools`. ``args_schema`` is the Pydantic
      JSON-Schema (so the UI can render a form), and ``invoke_url`` always
      points at this app's invoke endpoint.
    * ``api_agent`` — a runtime HTTP endpoint registered by the workflow
      "attach agents" flow. ``args_schema`` mirrors the call envelope and
      ``endpoint_id`` is the FK-like reference into the legacy table.
    """

    KIND_LANGCHAIN = "langchain"
    KIND_API_AGENT = "api_agent"
    KIND_CHOICES = [
        (KIND_LANGCHAIN, "LangChain tool"),
        (KIND_API_AGENT, "Runtime API agent"),
    ]

    name = models.SlugField(max_length=128, unique=True)
    display_name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    kind = models.CharField(
        max_length=16, choices=KIND_CHOICES, default=KIND_LANGCHAIN, db_index=True,
    )
    # Always set. For langchain tools this is ``/api/agent-tools/{name}/invoke``;
    # for api_agent rows the SPA may also POST to the upstream URL directly.
    invoke_url = models.CharField(max_length=2048, blank=True, default="")
    # Pydantic JSON-Schema for the tool's input model. Used to drive the
    # ToolInvokeModal form and any agent-side argument validation.
    args_schema = models.JSONField(default=dict, blank=True)
    # Free-form bag for langchain-side metadata (tags, return-type hint, ...)
    # or api_agent-side per-call defaults.
    metadata = models.JSONField(default=dict, blank=True)
    # Only set when ``kind='api_agent'``. References the legacy
    # ``api_agent_endpoints.id`` registered by ApiAgentPipeline.
    endpoint_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        # Physical table lives in the dedicated ``agent_tools`` Postgres
        # schema; the connection-level search_path
        # (``-c search_path=public,agent_tools``) resolves the bare name.
        # Keeping the db_table unqualified means Django's introspection
        # (used by `migrate` and the test DB flush) recognises the table.
        db_table = "tool"
        ordering = ["display_name", "name"]
        indexes = [models.Index(fields=["kind", "is_active"])]

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return f"{self.display_name} ({self.name})"


# ── External MCP server config ─────────────────────────────────────────────────


class McpServerConfig(_UUIDPK, _Timestamps):
    """Shared connection config for an external (mock or real) claims MCP/REST
    server that tools are routed to.

    Only the **base endpoint** is stored here (once). Each tool stores **only
    its path** in ``Tool.metadata['mcp_path']`` (e.g. ``/tools/facets_get_summary``).
    The execution engine joins ``base_url + path`` at call time.

    Exactly one row is normally active (``is_active=True``); the engine uses the
    most recently updated active row. When no active row exists tools fall back
    to their in-process ``agent_tools`` implementation (backward compatible).
    """

    label = models.CharField(max_length=128, default="claims-mock-mcp")
    # Base endpoint, no trailing slash, e.g. https://claims-mock-mcp-server.toystack.dev
    base_url = models.CharField(max_length=2048)
    # Auth header sent on every call (mock uses ``x-api-key``).
    auth_header = models.CharField(max_length=64, default="x-api-key")
    api_key = models.CharField(max_length=512, blank=True, default="")
    # HTTP verb + arg name the server expects for the claim identifier.
    http_method = models.CharField(max_length=8, default="POST")
    claim_arg = models.CharField(max_length=64, default="claim_number")
    timeout_seconds = models.PositiveIntegerField(default=30)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "mcp_server_config"
        ordering = ["-is_active", "-updated_at"]

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return f"{self.label} ({self.base_url})"


class SopFieldMapping(_UUIDPK, _Timestamps):
    """DB-backed canonical SOP field mapping (replaces ``yaml/sop_field_mapping.yaml``
    as the runtime source of truth).

    One row per canonical SOP business field ("Provider NPI", "Received Date",
    …). ``systems`` holds the per-source-system key lists the resolver searches,
    e.g.::

        {"DOC360": ["24 RENDERING NPI"], "FACETS": ["PRPR_NPI"],
         "CBS": [], "CBD": [], "NPI": []}

    The execution engine reads active rows via
    :func:`uhc_execution_engine.field_mapping._load` (DB-first, YAML fallback for
    standalone/CLI use). Edited from the builder UI; a ``post_save``/``post_delete``
    signal clears the engine's in-process cache, and a cheap (count, max
    updated_at) watermark makes edits visible to other processes (Celery workers)
    without a restart.
    """

    sop_field = models.CharField(max_length=255, unique=True, db_index=True)
    # Human-readable business meaning, shown in the UI so a non-engineer
    # understands what the cryptic source keys (e.g. "24 RENDERING NPI") mean.
    description = models.TextField(blank=True, default="")
    # Optional grouping for the UI (e.g. "Provider", "Member", "Coverage").
    category = models.CharField(max_length=128, blank=True, default="")
    # {"DOC360": [...], "FACETS": [...], "CBS": [...], "CBD": [...], "NPI": [...]}
    systems = models.JSONField(default=dict)
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "sop_field_mapping"
        ordering = ["category", "sop_field"]

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return self.sop_field


class ClaimOntologyField(_UUIDPK, _Timestamps):
    """DB-backed CMS-1500 claim ontology (replaces ``yaml/claim_ontology.yaml``).

    One row per canonical claim-image field, grouped by ``namespace`` (the YAML's
    top-level groups: ``header``, ``person_blocks``, ``addresses`` …). ``aliases``
    is the list of raw labels the parser normalises onto ``canonical_field``.

    Same DB-first / YAML-fallback + cache-invalidation story as
    :class:`SopFieldMapping`.
    """

    namespace = models.CharField(max_length=128, db_index=True)
    canonical_field = models.CharField(max_length=255, db_index=True)
    description = models.TextField(blank=True, default="")
    aliases = models.JSONField(default=list)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "claim_ontology_field"
        ordering = ["namespace", "canonical_field"]
        constraints = [
            models.UniqueConstraint(
                fields=["namespace", "canonical_field"],
                name="uniq_ontology_namespace_field",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return f"{self.namespace}:{self.canonical_field}"


# ── Node bindings ─────────────────────────────────────────────────────────────


class NodeRuleBinding(_UUIDPK, _Timestamps):
    """
    One SOP rule attached to one canvas shape.

    Replaces an entry inside ``Shape.properties.sop_rules``. The full
    ``AttachableSopRule`` shape that the UI sees is reconstituted by the
    ``/api/builder/workflows/:id/attachable`` view from the live SOP graph;
    only the fields the auditor edits / overrides on the shape itself are
    persisted here.
    """

    shape = models.ForeignKey(
        "builder.Shape", on_delete=models.CASCADE, related_name="rule_bindings",
    )
    sop = models.ForeignKey(
        "sop_ingestion.AuditSop", on_delete=models.CASCADE,
        related_name="rule_bindings",
    )
    # The opaque key produced by builder.views.attachable, e.g.
    # ``pre:{sop_id}:{precondition_id}:{idx}`` or
    # ``step:{sop_id}:{step_number}:{row_index}``.
    rule_key = models.CharField(max_length=255, db_index=True)
    # Auditor-editable fields (start as a copy of the SOP's authoritative
    # condition/action and may be overridden on the shape).
    condition = models.TextField(blank=True, default="")
    action = models.TextField(blank=True, default="")
    # JSON lists of rule_keys (other rule keys this rule depends on / is
    # excluded by, snapshotted at attach time).
    references_json = models.JSONField(default=list, blank=True)
    excluded_by_json = models.JSONField(default=list, blank=True)
    # The HTML reference snapshot the SPA renders next to the rule.
    html_reference_json = models.JSONField(default=dict, blank=True)
    ordering = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "node_rule_binding"
        ordering = ["shape", "ordering", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["shape", "rule_key"],
                name="uniq_node_rule_binding_shape_rule",
            ),
        ]
        indexes = [
            models.Index(fields=["shape", "ordering"]),
            models.Index(fields=["sop", "rule_key"]),
        ]


class NodeToolBinding(_UUIDPK, _Timestamps):
    """
    One tool call attached to one canvas shape.

    The optional ``rule_binding`` FK is the "this tool was picked while the
    user was attaching that rule" relationship. The plan calls this shape
    *tools_flat_with_rule_ref*: rules and tools both surface independently
    on the node, and the UI groups "tools for rule X" by joining on this
    FK.
    """

    shape = models.ForeignKey(
        "builder.Shape", on_delete=models.CASCADE, related_name="tool_bindings",
    )
    tool = models.ForeignKey(
        Tool, on_delete=models.PROTECT, related_name="bindings",
    )
    # Default arguments to pre-fill in the invoke form for this shape.
    args_template = models.JSONField(default=dict, blank=True)
    rule_binding = models.ForeignKey(
        NodeRuleBinding,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="tool_bindings",
    )
    ordering = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "node_tool_binding"
        ordering = ["shape", "ordering", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["shape", "tool", "rule_binding"],
                name="uniq_node_tool_binding_shape_tool_rule",
            ),
        ]
        indexes = [
            models.Index(fields=["shape", "ordering"]),
            models.Index(fields=["tool"]),
            models.Index(fields=["rule_binding"]),
        ]
