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


def _rule_keys_out_of_scope(rule_keys) -> set[str]:
    """Return the subset of ``rule_keys`` whose SOP step/decision is out of scope.

    A rule_key is ``step:<sop_id>:<step_no>:<row_index>`` for decision rules.
    OOS is true when either the AuditStep or its AuditDecision is flagged
    ``is_out_of_scope`` (mirrors ``rule_loader._hydrate_decision``).
    Preconditions (``pre:...``) are never out of scope.
    """
    oos: set[str] = set()
    try:
        from sop_ingestion.models import AuditDecision  # local import
    except Exception:
        return oos

    for key in rule_keys:
        m = re.match(r"^step:(\d+):(\d+):(\d+)$", key or "")
        if not m:
            continue
        sop_id, step_no, row_index = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        try:
            dec = (
                AuditDecision.objects
                .select_related("step")
                .filter(
                    step__sop_id=sop_id,
                    step__step_number=step_no,
                    row_index=row_index,
                )
                .first()
            )
        except Exception:
            dec = None
        if dec is not None and (dec.is_out_of_scope or dec.step.is_out_of_scope):
            oos.add(key)
    return oos


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

    raw_rules = [r for r in (props.get("sop_rules") or []) if isinstance(r, dict)]
    if rule_rows or raw_rules:
        # Build a lookup of the raw JSONB rules by key so we can preserve
        # fields not stored in NodeRuleBinding (sop_title, source,
        # section_label, section_narrative, decision_type, codes, etc.) AND so
        # user-authored custom rules — which have no SOP and therefore never
        # become NodeRuleBinding rows — survive the round-trip instead of being
        # overwritten by the binding-derived list.
        raw_by_key: dict[str, dict] = {
            r["key"]: r for r in raw_rules if r.get("key")
        }
        bound_by_key = {row.rule_key: row for row in rule_rows}
        oos_keys = _rule_keys_out_of_scope(list(bound_by_key))

        def _binding_entry(row) -> dict:
            entry = dict(raw_by_key.get(row.rule_key) or {})
            # Authoritative fields from the binding row always win.
            entry.update({
                "id":             str(row.id),
                "key":            row.rule_key,
                "sop_id":         row.sop_id,
                "condition":      row.condition,
                "action":         row.action,
                "references":     row.references_json or [],
                "excluded_by":    row.excluded_by_json or [],
                "html_reference": row.html_reference_json or {},
                "ordering":       row.ordering,
                "is_out_of_scope": row.rule_key in oos_keys,
            })
            return entry

        merged: list[dict] = []
        seen_bound: set[str] = set()
        # Walk the saved order so custom rules keep their position relative to
        # the SOP rules the auditor interleaved them with.
        for raw in raw_rules:
            key = raw.get("key") or ""
            row = bound_by_key.get(key)
            if row is not None:
                merged.append(_binding_entry(row))
                seen_bound.add(key)
            else:
                # Unbound rule = custom (no SOP). Keep it exactly as authored.
                entry = dict(raw)
                entry["is_custom"] = bool(raw.get("is_custom")) or key.startswith("custom:")
                merged.append(entry)
        # Defensive: surface any binding rows the raw list didn't mention.
        for row in rule_rows:
            if row.rule_key not in seen_bound:
                merged.append(_binding_entry(row))

        # Renormalise ordering to the final merged sequence.
        for i, entry in enumerate(merged):
            entry["ordering"] = i

        props["sop_rules"] = merged

        # Shape-level rollup so the canvas can flag the node without having to
        # inspect every rule. ``is_out_of_scope`` is true only when EVERY rule
        # is an out-of-scope (clean-exclusion) rule; ``oos_rule_count`` /
        # ``rule_count`` let the UI mark partially-OOS nodes too.
        props["oos_rule_count"] = len(oos_keys)
        props["rule_count"] = len(merged)
        props["is_out_of_scope"] = bool(oos_keys) and len(oos_keys) == len(merged)

    # ── Manual per-node override (auditor-set on the canvas) ──────────────────
    # ``manual_out_of_scope`` is a user toggle that excludes the WHOLE node from
    # the execution engine regardless of the SOP-derived rollup above, and works
    # even for custom / rule-less nodes (which never produce ``oos_keys``). It is
    # stored verbatim in ``shape.properties`` (round-trips via the graph save) so
    # we simply preserve it and fold it into the effective ``is_out_of_scope``
    # the canvas badge reads. Kept on a SEPARATE key so a save never clobbers the
    # auditor's choice with the computed rollup.
    if bool(props.get("manual_out_of_scope")):
        props["is_out_of_scope"] = True

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
