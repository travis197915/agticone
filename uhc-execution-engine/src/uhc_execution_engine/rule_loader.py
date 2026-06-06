"""Load every NodeRuleBinding / NodeToolBinding for a workflow and hydrate
them into the same dict shape that ``builder.views.attachable`` returns.

We intentionally re-derive ``decision_type``, ``codes`` and ``references``
from the live SOP rows so the engine stays consistent with the SPA picker —
the binding row only carries the auditor-editable overrides.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _split_key(rule_key: str) -> tuple[str, list[str]]:
    """Return (kind, parts) where kind is 'pre' or 'step'."""
    parts = rule_key.split(":")
    if not parts:
        return "", []
    return parts[0], parts[1:]


def _hydrate_precondition(sop, pc, idx: int, override_condition: str,
                          override_action: str) -> dict[str, Any]:
    rules = pc.llm_rules or []
    rule = rules[idx] if 0 <= idx < len(rules) else {}
    return {
        "key":               f"pre:{sop.id}:{pc.id}:{idx}",
        "sop_id":            sop.id,
        "sop_title":         sop.title or f"SOP #{sop.id}",
        "source":            "precondition",
        "section_id":        pc.id,
        "section_label":     pc.label or pc.category,
        "section_category":  pc.category,
        "yaml_rule_id":      "",
        "subrule_id":        "",
        "step_question":     pc.label or "",
        "section_narrative": pc.content_text or "",
        "condition":         (override_condition or (rule.get("condition") or "")).strip(),
        "action":            (override_action or (rule.get("action") or "")).strip(),
        "decision_type":     (rule.get("decision_type") or "").strip(),
        "is_exception":      bool(rule.get("is_exception")),
        "codes":             [],
        "is_blocking":       pc.is_blocking,
        # ── Routing metadata (preconditions never route; safe defaults) ──
        "step_number":       None,
        "is_out_of_scope":   False,
        "is_final":          False,
        "aggregation":       "LEAF",
        "applicable_when":   "",
        "goto_step":         None,
    }


def _hydrate_decision(sop, step, dec, override_condition: str,
                      override_action: str) -> dict[str, Any]:
    codes = list(dec.all_codes or []) or [
        *(dec.eob_codes or []),
        *(dec.ex_codes or []),
        *(dec.denial_codes or []),
        *(dec.system_actions or []),
    ]
    cond_parts = [p for p in [dec.condition_if, dec.condition_and] if p]
    section_label = f"Step {step.step_number}"
    if step.question:
        section_label += f": {step.question}"
    return {
        "key":               f"step:{sop.id}:{step.step_number}:{dec.row_index}",
        "sop_id":            sop.id,
        "sop_title":         sop.title or f"SOP #{sop.id}",
        "source":            "decision",
        "section_id":        step.step_number,
        "section_label":     section_label,
        "section_category":  "DECISION",
        "yaml_rule_id":      step.yaml_rule_id or "",
        "subrule_id":        dec.subrule_id or "",
        "step_question":     step.question or "",
        "section_narrative": step.narrative_context or step.intro_text or "",
        "condition":         (override_condition or " AND ".join(cond_parts)).strip(),
        "action":            (override_action or dec.action_text or dec.action_summary or "").strip(),
        "decision_type":     dec.decision_type or "",
        "is_exception":      False,
        "codes":             codes,
        "is_blocking":       dec.is_final,
        # ── Routing metadata consumed by execute_shapes' step cursor ──
        "step_number":       step.step_number,
        "is_out_of_scope":   bool(dec.is_out_of_scope or step.is_out_of_scope),
        "is_final":          bool(dec.is_final),
        "aggregation":       dec.aggregation or "LEAF",
        "applicable_when":   (getattr(dec, "applicable_when", "") or "").strip(),
        "goto_step":         dec.goto_step,
    }


def load_workflow_bindings(workflow_id: str) -> dict[str, Any]:
    """Return preconditions, decisions, and tool binding lookups for a workflow.

    Output shape::

        {
            "preconditions": [rule_dict, ...],     # ordered by binding.ordering
            "decisions":     [rule_dict, ...],
            "tools_by_rule_key": {rule_key: [tool_binding_dict, ...]},
            "tools_by_shape":    {shape_id_str: [tool_binding_dict, ...]},
            "all_tool_bindings": [tool_binding_dict, ...],  # ordered, deduped
            "shapes": [
                {
                    "shape_id":      str,
                    "shape_label":   str,
                    "workbench":     {"name": str, "order": int},
                    "shape_order":   int,
                    "rules":         [rule_dict, ...],     # preconditions + decisions on this shape
                    "tool_bindings": [tool_binding_dict, ...],
                },
                ...
            ],  # ordered by (workbench.order, shape.order); only shapes that have
                # at least one rule binding are included.
        }
    """
    from agent_tools.models import NodeRuleBinding, NodeToolBinding
    from sop_ingestion.models import (AuditSop, AuditPrecondition,
                                       AuditStep, AuditDecision)

    rule_bindings = list(
        NodeRuleBinding.objects
        .filter(shape__workbench__work_area__workflow_id=workflow_id)
        .select_related("sop", "shape", "shape__workbench")
        .order_by("shape__workbench__order", "shape__order", "ordering", "created_at")
    )
    tool_bindings = list(
        NodeToolBinding.objects
        .filter(shape__workbench__work_area__workflow_id=workflow_id)
        .select_related("tool", "shape", "rule_binding")
        .order_by("shape__workbench__order", "shape__order", "ordering", "created_at")
    )

    # Bulk-load SOP rows referenced by the bindings to avoid N+1 queries.
    sop_ids = {rb.sop_id for rb in rule_bindings}
    sops = {s.id: s for s in AuditSop.objects.filter(id__in=sop_ids)}
    pre_ids = {int(rb.rule_key.split(":")[2]) for rb in rule_bindings
               if rb.rule_key.startswith("pre:") and len(rb.rule_key.split(":")) >= 4}
    preconditions = {pc.id: pc for pc in AuditPrecondition.objects.filter(id__in=pre_ids)}

    # Index AuditDecision by (sop_id, step_number, row_index) for fast lookup.
    decision_index: dict[tuple[int, int, int], tuple[Any, Any]] = {}
    step_index: dict[tuple[int, int], Any] = {}
    if sop_ids:
        for step in (AuditStep.objects
                     .filter(sop_id__in=sop_ids)
                     .prefetch_related("decisions")):
            step_index[(step.sop_id, step.step_number)] = step
            for dec in step.decisions.all():
                decision_index[(step.sop_id, step.step_number, dec.row_index)] = (step, dec)

    preconds_out: list[dict[str, Any]] = []
    decisions_out: list[dict[str, Any]] = []
    # Per-shape grouping. Insertion-ordered dict keyed by shape_id so the
    # final list preserves the SQL ordering (workbench.order, shape.order).
    shapes_by_id: dict[str, dict[str, Any]] = {}
    for rb in rule_bindings:
        kind, parts = _split_key(rb.rule_key)
        sop = sops.get(rb.sop_id)
        if sop is None:
            logger.warning("rule_loader: missing sop %s for binding %s", rb.sop_id, rb.id)
            continue
        try:
            if kind == "pre" and len(parts) >= 3:
                pc_id, idx = int(parts[1]), int(parts[2])
                pc = preconditions.get(pc_id)
                if pc is None:
                    logger.warning("rule_loader: precondition %s missing", pc_id)
                    continue
                rule_dict = _hydrate_precondition(sop, pc, idx, rb.condition, rb.action)
            elif kind == "step" and len(parts) >= 3:
                step_no, row_idx = int(parts[1]), int(parts[2])
                lookup = decision_index.get((rb.sop_id, step_no, row_idx))
                if lookup is None:
                    logger.warning("rule_loader: decision %s missing", rb.rule_key)
                    continue
                step, dec = lookup
                rule_dict = _hydrate_decision(sop, step, dec, rb.condition, rb.action)
            else:
                logger.warning("rule_loader: unrecognised rule_key %s", rb.rule_key)
                continue
        except (ValueError, IndexError) as exc:
            logger.warning("rule_loader: bad rule_key %s (%s)", rb.rule_key, exc)
            continue

        rule_dict["binding_id"] = str(rb.id)
        rule_dict["shape_id"] = str(rb.shape_id)
        rule_dict["references"] = list(rb.references_json or [])
        rule_dict["excluded_by"] = list(rb.excluded_by_json or [])
        if kind == "pre":
            preconds_out.append(rule_dict)
        else:
            decisions_out.append(rule_dict)

        # Capture per-shape grouping. The first binding we see for a given
        # shape provides the shape/workbench metadata (cheap, since we
        # select_related'd them in the queryset).
        shape_id_str = str(rb.shape_id)
        shape_group = shapes_by_id.get(shape_id_str)
        if shape_group is None:
            shape = rb.shape
            workbench = shape.workbench
            shape_group = {
                "shape_id":      shape_id_str,
                "shape_label":   shape.label or "",
                "workbench":     {"name": workbench.name or "", "order": workbench.order},
                "shape_order":   shape.order,
                "rules":         [],
                "tool_bindings": [],
            }
            shapes_by_id[shape_id_str] = shape_group
        shape_group["rules"].append(rule_dict)

    # Tool binding dicts + scoping lookups
    def _tool_dict(tb) -> dict[str, Any]:
        return {
            "binding_id":      str(tb.id),
            "shape_id":        str(tb.shape_id),
            "rule_binding_id": str(tb.rule_binding_id) if tb.rule_binding_id else "",
            "tool_name":       tb.tool.name,
            "tool_kind":       tb.tool.kind,
            "invoke_url":      tb.tool.invoke_url,
            "args_template":   dict(tb.args_template or {}),
        }

    all_tool_bindings = [_tool_dict(tb) for tb in tool_bindings]

    # Map binding_id -> rule_key for rule_binding FK lookups
    rb_id_to_key = {str(rb.id): rb.rule_key for rb in rule_bindings}
    tools_by_rule_key: dict[str, list[dict[str, Any]]] = {}
    tools_by_shape: dict[str, list[dict[str, Any]]] = {}
    for td in all_tool_bindings:
        tools_by_shape.setdefault(td["shape_id"], []).append(td)
        if td["rule_binding_id"]:
            rk = rb_id_to_key.get(td["rule_binding_id"])
            if rk:
                tools_by_rule_key.setdefault(rk, []).append(td)
        # Attach to the per-shape grouping when that shape carries rules.
        shape_group = shapes_by_id.get(td["shape_id"])
        if shape_group is not None:
            shape_group["tool_bindings"].append(td)

    return {
        "preconditions": preconds_out,
        "decisions": decisions_out,
        "tools_by_rule_key": tools_by_rule_key,
        "tools_by_shape": tools_by_shape,
        "all_tool_bindings": all_tool_bindings,
        "shapes": list(shapes_by_id.values()),
    }
