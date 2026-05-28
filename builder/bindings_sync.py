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

    Uses upsert (update_or_create) for NodeRuleBinding so that existing rows
    keep their UUIDs across saves.  This prevents an IntegrityError from
    ``execution_rule_evaluation``, which holds a FK to ``node_rule_binding``
    and cannot be satisfied by a wipe-and-reinsert strategy.

    Stale bindings (rules removed from the shape) are deleted individually;
    rows that are still referenced by execution history are left in place with
    a warning rather than aborting the entire save.

    Safe to call when agent_tools is unavailable — in that case we simply
    leave the JSON blobs alone.
    """
    Tool, NodeRuleBinding, NodeToolBinding = _safe_import_agent_tools()
    if NodeRuleBinding is None or NodeToolBinding is None:
        return

    props = shape.properties or {}
    raw_rules = props.get("sop_rules") or []
    raw_tools = props.get("tool_calls") or []

    # Durable per-rule manual out-of-scope set. The auditor can toggle OOS on an
    # individual rule / sub-rule / sub-sub-rule (each is a distinct decision row
    # with a unique ``key``), via ``sop_rules[i].manual_out_of_scope``. The
    # NodeRuleBinding table has no per-row OOS column, so we persist the set of
    # flagged rule_keys on the shape itself — it survives the binding round-trip
    # and drives both hydration (badge) and execution (skip). The per-entry flag
    # is the single source of truth; we recompute the list from it on every save.
    manual_oos_keys = sorted({
        str(r.get("key"))
        for r in raw_rules
        if isinstance(r, dict) and r.get("key") and r.get("manual_out_of_scope")
    })
    # Force-IN-scope override set: rules the auditor pulled back into scope even
    # though the SOP (or node) flagged them out of scope. This BEATS the SOP flag
    # during hydration and execution, so an ingestion-out-of-scope rule can be
    # re-enabled. ``manual_in_scope`` and ``manual_out_of_scope`` are mutually
    # exclusive per rule (the UI sets one true, the other false).
    manual_in_keys = sorted({
        str(r.get("key"))
        for r in raw_rules
        if isinstance(r, dict) and r.get("key") and r.get("manual_in_scope")
    })
    changed = False
    if (props.get("manual_oos_rule_keys") or []) != manual_oos_keys:
        props["manual_oos_rule_keys"] = manual_oos_keys
        changed = True
    if (props.get("manual_in_scope_rule_keys") or []) != manual_in_keys:
        props["manual_in_scope_rule_keys"] = manual_in_keys
        changed = True
    if changed:
        shape.properties = props
        try:
            shape.save(update_fields=["properties"])
        except Exception as exc:
            logger.warning("agent_tools: could not persist manual scope override "
                           "keys (shape=%s): %s", shape.id, exc)

    # ── Rule bindings: upsert to preserve IDs referenced by execution history ─
    rule_binding_by_key: dict[str, Any] = {}
    incoming_rule_keys: set[str] = set()

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
            row, _ = NodeRuleBinding.objects.update_or_create(
                shape=shape,
                rule_key=rule_key,
                defaults=dict(
                    sop=sop,
                    condition=rule.get("condition", "") or "",
                    action=rule.get("action", "") or "",
                    references_json=list(rule.get("references") or []),
                    excluded_by_json=list(rule.get("excluded_by") or []),
                    html_reference_json=rule.get("html_reference") or {},
                    ordering=idx,
                ),
            )
        except Exception as exc:
            logger.warning(
                "agent_tools: could not upsert NodeRuleBinding "
                "(shape=%s, rule_key=%s): %s",
                shape.id, rule_key, exc,
            )
            continue
        incoming_rule_keys.add(rule_key)
        rule_binding_by_key[rule_key] = row

    # Delete stale bindings (rules removed from this shape).  Delete
    # individually so a FK violation on execution_rule_evaluation is caught
    # per-row — a referenced binding is left in place rather than blocking the
    # entire canvas save.
    stale_qs = NodeRuleBinding.objects.filter(shape=shape).exclude(
        rule_key__in=incoming_rule_keys
    )
    for stale in stale_qs:
        try:
            stale.delete()
        except Exception as exc:
            logger.warning(
                "agent_tools: could not delete stale NodeRuleBinding "
                "(id=%s, rule_key=%s) — still referenced by execution history: %s",
                stale.id, stale.rule_key, exc,
            )

    # ── Tool bindings: safe to wipe-and-reinsert (FK uses SET_NULL) ───────────
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


def out_of_scope_keys_for_shapes(shapes) -> set[str]:
    """Resolve the out-of-scope rule_keys for many shapes in ONE query.

    Collects every bound rule_key across ``shapes`` (using prefetched
    ``rule_bindings`` when available) and resolves them all together, so the
    graph serializer can compute OOS once instead of once per shape.
    """
    keys: list[str] = []
    for shape in shapes:
        try:
            keys.extend(r.rule_key for r in shape.rule_bindings.all())
        except Exception:
            continue
    return _rule_keys_out_of_scope(keys)


def _rule_keys_out_of_scope(rule_keys) -> set[str]:
    """Return the subset of ``rule_keys`` whose SOP step/decision is out of scope.

    A rule_key is ``step:<sop_id>:<step_no>:<row_index>`` for decision rules.
    OOS is true when either the AuditStep or its AuditDecision is flagged
    ``is_out_of_scope`` (mirrors ``rule_loader._hydrate_decision``).
    Preconditions (``pre:...``) are never out of scope.

    Resolved in a SINGLE batched query (no N+1): every relevant decision is
    fetched once and matched in memory. This is the hot path during graph GET/
    PUT hydration — one query per call instead of one per rule key.
    """
    oos: set[str] = set()
    try:
        from sop_ingestion.models import AuditDecision  # local import
    except Exception:
        return oos

    parsed: dict[tuple[int, int, int], str] = {}
    sop_ids: set[int] = set()
    step_nos: set[int] = set()
    for key in rule_keys:
        m = re.match(r"^step:(\d+):(\d+):(\d+)$", key or "")
        if not m:
            continue
        sop_id, step_no, row_index = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        parsed[(sop_id, step_no, row_index)] = key
        sop_ids.add(sop_id)
        step_nos.add(step_no)
    if not parsed:
        return oos

    try:
        rows = (
            AuditDecision.objects
            .filter(step__sop_id__in=sop_ids, step__step_number__in=step_nos)
            .values_list("step__sop_id", "step__step_number", "row_index",
                         "is_out_of_scope", "step__is_out_of_scope")
        )
    except Exception:
        return oos

    for sop_id, step_no, row_index, dec_oos, step_oos in rows:
        key = parsed.get((sop_id, step_no, row_index))
        if key and (dec_oos or step_oos):
            oos.add(key)
    return oos


def hydrate_properties_with_bindings(shape, oos_keys: set[str] | None = None) -> dict[str, Any]:
    """Return shape.properties augmented with sop_rules + tool_calls from DB.

    ``oos_keys`` (optional) lets a caller pass a pre-resolved set of out-of-scope
    rule_keys for the WHOLE workflow so the per-shape OOS query is skipped — this
    is the hot path for graph GET/PUT, where computing it once and reusing it
    avoids one AuditDecision query per shape. When omitted it is resolved here so
    single-shape callers keep working unchanged.
    """
    props = dict(shape.properties or {})
    Tool, NodeRuleBinding, NodeToolBinding = _safe_import_agent_tools()
    if NodeRuleBinding is None or NodeToolBinding is None:
        return props

    # Use the related manager (not a fresh filter) so a prefetched queryset on
    # the shape is reused instead of issuing a query; sort in Python to keep the
    # prefetch cache intact. Falls back to a query when not prefetched.
    try:
        rule_rows = sorted(shape.rule_bindings.all(), key=lambda r: r.ordering)
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
        if oos_keys is None:
            oos_keys = _rule_keys_out_of_scope(list(bound_by_key))
        # Auditor's per-rule manual OOS toggles (rules / sub-rules / sub-sub-
        # rules), persisted on write. Re-emitted per entry so the toggle state
        # round-trips, and OR'd into the effective ``is_out_of_scope``.
        manual_keys = set(props.get("manual_oos_rule_keys") or [])
        manual_in_keys = set(props.get("manual_in_scope_rule_keys") or [])

        def _binding_entry(row) -> dict:
            entry = dict(raw_by_key.get(row.rule_key) or {})
            is_manual = row.rule_key in manual_keys
            forced_in = row.rule_key in manual_in_keys
            # Effective scope: manual OOS or SOP-derived OOS, UNLESS the auditor
            # forced the rule back in scope (force-in wins over everything).
            effective_oos = (is_manual or (row.rule_key in oos_keys)) and not forced_in
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
                "manual_out_of_scope": is_manual,
                "manual_in_scope": forced_in,
                "is_out_of_scope": effective_oos,
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
                # Unbound rule = custom (no SOP). Keep it exactly as authored,
                # but still honor a manual OOS toggle on it.
                entry = dict(raw)
                entry["is_custom"] = bool(raw.get("is_custom")) or key.startswith("custom:")
                is_manual = key in manual_keys
                forced_in = key in manual_in_keys
                entry["manual_out_of_scope"] = is_manual
                entry["manual_in_scope"] = forced_in
                entry["is_out_of_scope"] = (
                    (bool(entry.get("is_out_of_scope")) or is_manual) and not forced_in
                )
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
        # inspect every rule. Effective OOS now includes SOP-derived rows, manual
        # per-rule toggles, and custom rules. ``is_out_of_scope`` is true only
        # when EVERY rule is out of scope; ``oos_rule_count`` / ``rule_count``
        # let the UI mark partially-OOS nodes too.
        effective_oos = sum(1 for e in merged if e.get("is_out_of_scope"))
        props["oos_rule_count"] = effective_oos
        props["rule_count"] = len(merged)
        props["is_out_of_scope"] = len(merged) > 0 and effective_oos == len(merged)

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
        tool_rows = sorted(shape.tool_bindings.all(), key=lambda r: r.ordering)
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
