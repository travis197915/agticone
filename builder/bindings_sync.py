"""
Bridge between the legacy ``Shape.properties.{sop_rules, tool_calls}``
JSON-blob shape and the relational ``NodeRuleBinding`` /
``NodeToolBinding`` tables in :mod:`agent_tools`.

Two flows, both used by ``builder.services.WorkflowGraphWriter``:

* :func:`extract_bindings_from_properties` — called per-shape during PUT.
  Reads ``shape.properties.sop_rules`` and ``shape.properties.tool_calls``
  and writes them to the DB. Also mirrors a thin summary back into
  ``properties`` for the one-release back-compat window the plan calls out.

* :func:`hydrate_properties_with_bindings` — called per-shape during GET
  so the SPA sees the same envelope whether the data came from old JSON
  blobs or new binding rows.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _safe_import_agent_tools():
    """Return ``(Tool, NodeRuleBinding, NodeToolBinding)`` or all-None."""
    try:
        from agent_tools.models import NodeRuleBinding, NodeToolBinding, Tool
    except Exception:
        return None, None, None
    return Tool, NodeRuleBinding, NodeToolBinding


def _rule_sop_id(rule_key: str) -> int | None:
    """Extract the SOP id encoded in the rule_key produced by `attachable`."""
    m = re.match(r"^(?:pre|step):(\d+):", rule_key or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _resolve_sop(sop_id: int):
    try:
        from sop_ingestion.models import AuditSop
        return AuditSop.objects.filter(id=sop_id).first()
    except Exception:
        return None


def _resolve_tool(payload: dict[str, Any]):
    """Resolve a Tool row from one ``tool_calls[]`` entry."""
    Tool, *_ = _safe_import_agent_tools()
    if Tool is None:
        return None
    tool_id = payload.get("tool_id") or payload.get("toolId")
    if tool_id:
        try:
            return Tool.objects.filter(id=tool_id).first()
        except Exception:
            pass
    name = payload.get("name") or payload.get("tool_name")
    if name:
        return Tool.objects.filter(name=name).first()
    endpoint = payload.get("endpoint_id") or payload.get("endpointId")
    if endpoint:
        return Tool.objects.filter(endpoint_id=endpoint).first()
    return None


# ── Write path: properties → binding tables ─────────────────────────────────


def extract_bindings_from_properties(shape) -> None:
    """Project ``shape.properties`` into NodeRuleBinding + NodeToolBinding rows.

    Wipe-and-reinsert per shape (the canvas PUT is already a full
    snapshot). Safe to call when agent_tools is unavailable — in that
    case we simply leave the JSON blobs alone.
    """
    Tool, NodeRuleBinding, NodeToolBinding = _safe_import_agent_tools()
    if NodeRuleBinding is None or NodeToolBinding is None:
        return

    props = shape.properties or {}
    raw_rules = props.get("sop_rules") or []
    raw_tools = props.get("tool_calls") or []

    NodeRuleBinding.objects.filter(shape=shape).delete()
    rule_binding_by_key: dict[str, Any] = {}
    for idx, rule in enumerate(raw_rules):
        if not isinstance(rule, dict):
            continue
        rule_key = rule.get("key") or ""
        if not rule_key:
            continue
        sop_id = rule.get("sop_id") or _rule_sop_id(rule_key)
        sop = _resolve_sop(sop_id) if sop_id else None
        if sop is None:
            continue
        try:
            row = NodeRuleBinding.objects.create(
                shape=shape, sop=sop, rule_key=rule_key,
                condition=rule.get("condition", "") or "",
                action=rule.get("action", "") or "",
                references_json=list(rule.get("references") or []),
                excluded_by_json=list(rule.get("excluded_by") or []),
                html_reference_json=rule.get("html_reference") or {},
                ordering=idx,
            )
        except Exception as exc:
            logger.warning(
                "agent_tools: could not persist NodeRuleBinding "
                "(shape=%s, rule_key=%s): %s",
                shape.id, rule_key, exc,
            )
            continue
        rule_binding_by_key[rule_key] = row

    NodeToolBinding.objects.filter(shape=shape).delete()
    for idx, tool_call in enumerate(raw_tools):
        if not isinstance(tool_call, dict):
            continue
        tool = _resolve_tool(tool_call)
        if tool is None:
            continue
        rule_binding_id = tool_call.get("rule_binding_id") or tool_call.get("ruleBindingId")
        rule_binding = None
        if rule_binding_id:
            rule_binding = NodeRuleBinding.objects.filter(
                id=rule_binding_id, shape=shape,
            ).first()
        if rule_binding is None:
            picked_rule_key = tool_call.get("rule_key") or tool_call.get("for_rule_key")
            if picked_rule_key:
                rule_binding = rule_binding_by_key.get(picked_rule_key)
        try:
            NodeToolBinding.objects.create(
                shape=shape, tool=tool,
                args_template=tool_call.get("args_template")
                    or tool_call.get("argsTemplate") or {},
                rule_binding=rule_binding,
                ordering=idx,
            )
        except Exception as exc:
            logger.warning(
                "agent_tools: could not persist NodeToolBinding "
                "(shape=%s, tool=%s): %s",
                shape.id, getattr(tool, "name", "?"), exc,
            )


# ── Read path: binding tables → properties ──────────────────────────────────


def hydrate_properties_with_bindings(shape) -> dict[str, Any]:
    """Return shape.properties augmented with sop_rules + tool_calls from DB."""
    props = dict(shape.properties or {})
    Tool, NodeRuleBinding, NodeToolBinding = _safe_import_agent_tools()
    if NodeRuleBinding is None or NodeToolBinding is None:
        return props

    try:
        rule_rows = list(
            NodeRuleBinding.objects.filter(shape=shape).order_by("ordering")
        )
    except Exception:
        rule_rows = []

    if rule_rows:
        props["sop_rules"] = [{
            "id":              str(row.id),
            "key":             row.rule_key,
            "sop_id":          row.sop_id,
            "condition":       row.condition,
            "action":          row.action,
            "references":      row.references_json or [],
            "excluded_by":     row.excluded_by_json or [],
            "html_reference":  row.html_reference_json or {},
            "ordering":        row.ordering,
        } for row in rule_rows]

    try:
        tool_rows = list(
            NodeToolBinding.objects
            .filter(shape=shape)
            .select_related("tool", "rule_binding")
            .order_by("ordering")
        )
    except Exception:
        tool_rows = []

    if tool_rows:
        props["tool_calls"] = [{
            "id":              str(row.id),
            "tool_id":         str(row.tool.id),
            "name":            row.tool.name,
            "display_name":    row.tool.display_name,
            "description":     row.tool.description,
            "tool_kind":       row.tool.kind,
            "kind":            row.tool.kind,
            "invoke_url":      row.tool.invoke_url,
            "args_schema":     row.tool.args_schema or {},
            "args_template":   row.args_template or {},
            "endpoint_id":     row.tool.endpoint_id,
            "rule_binding_id": str(row.rule_binding_id) if row.rule_binding_id else None,
            "rule_key":        row.rule_binding.rule_key if row.rule_binding else None,
            "ordering":        row.ordering,
        } for row in tool_rows]

    return props
