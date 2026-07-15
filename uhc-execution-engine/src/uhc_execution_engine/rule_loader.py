"""Load every NodeRuleBinding / NodeToolBinding for a workflow and hydrate
them into the same dict shape that ``builder.views.attachable`` returns.

We intentionally re-derive ``decision_type``, ``codes`` and ``references``
from the live SOP rows so the engine stays consistent with the SPA picker —
the binding row only carries the auditor-editable overrides.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .lob import default_tool_lob_scope

logger = logging.getLogger(__name__)

_GOTO_VERB_RE = re.compile(
    r"(?:skip to|proceed to|go to|directly to|jump to)\s+step\s+(\d+)", re.I)
_GOTO_DIRECTLY_RE = re.compile(r"step\s+(\d+)\s+directly", re.I)


def _first_goto(*texts: str) -> int | None:
    """First explicit numbered routing target across the given text blobs."""
    blob = " ".join(t for t in texts if t)
    m = _GOTO_VERB_RE.search(blob) or _GOTO_DIRECTLY_RE.search(blob)
    return int(m.group(1)) if m else None


def _all_gotos(*texts: str) -> list[int]:
    """All explicit numbered routing targets (document order, de-duplicated)."""
    blob = " ".join(t for t in texts if t)
    seen: list[int] = []
    for rx in (_GOTO_VERB_RE, _GOTO_DIRECTLY_RE):
        for m in rx.finditer(blob):
            n = int(m.group(1))
            if n not in seen:
                seen.append(n)
    return seen


def _split_key(rule_key: str) -> tuple[str, list[str]]:
    """Return (kind, parts) where kind is 'pre' or 'step'."""
    parts = rule_key.split(":")
    if not parts:
        return "", []
    return parts[0], parts[1:]


def _workbench_extra_context(workbench) -> str:
    """Auditor-provided free-form context attached to a workbench (SOP column).

    Stored in ``Workbench.config['extra_context']`` via the builder UI and
    injected verbatim into every rule-evaluation prompt for the rules that
    belong to this SOP (see ``_eval_common._workbench_context_section``). It is
    guidance only — it never dictates a verdict — and is additive: an empty
    string leaves the prompt unchanged.
    """
    if workbench is None:
        return ""
    cfg = getattr(workbench, "config", None) or {}
    return str(cfg.get("extra_context") or "").strip()


def _workbench_lob_scope(workbench) -> list[str]:
    """LOBs this SOP (workbench column) applies to, from ``config['lob_scope']``.

    Optional auditor configuration. When non-empty, ``execute_shapes`` skips
    this SOP's rules (no LLM call) for any claim whose LOB is not listed — the
    "those rules are not in the workflow for this LOB, don't process" path. An
    empty list (the default) means the SOP applies to every LOB.
    """
    if workbench is None:
        return []
    cfg = getattr(workbench, "config", None) or {}
    raw = cfg.get("lob_scope") or cfg.get("lobs") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(x).strip() for x in raw if str(x).strip()]


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

    # Routing: prefer the persisted goto_step, else recover an explicit target
    # from ANY of the row's text fields (the LLM sometimes lands the phrase in
    # condition/description rather than the action).
    row_goto = dec.goto_step
    if row_goto is None:
        row_goto = _first_goto(
            dec.action_text or "", dec.action_summary or "",
            dec.condition_if or "", dec.condition_and or "",
            dec.output_text or "",
        )
    # Step-level routing carried only in the step narrative (e.g. a deny branch
    # whose "directly proceed to step 9" survives in intro_text). Surfaced as a
    # labeled field so the engine/UI can honour the step's default next-hop(s).
    step_narrative = " ".join(
        t for t in [step.intro_text, step.narrative_context, step.question] if t
    )
    step_gotos = _all_gotos(step_narrative)
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
        # EOB codes specifically (subset of ``codes``). A matched rule that
        # references an EOB code is an audit defect (per the verdict policy),
        # so the engine/aggregator need them called out separately.
        "eob_codes":         list(dec.eob_codes or []),
        "is_blocking":       dec.is_final,
        # ── Routing metadata consumed by execute_shapes' step cursor ──
        "step_number":       step.step_number,
        "is_out_of_scope":   bool(dec.is_out_of_scope or step.is_out_of_scope),
        "is_final":          bool(dec.is_final),
        "aggregation":       dec.aggregation or "LEAF",
        "applicable_when":   (getattr(dec, "applicable_when", "") or "").strip(),
        "goto_step":         row_goto,
        # Step's default next-hop(s) parsed from the narrative — used when no
        # row-level goto fires and rendered as a labeled "Step N" reference.
        "step_goto_step":    (step_gotos[0] if step_gotos else None),
        "step_goto_steps":   step_gotos,
        # ── Hierarchy metadata so the canvas/inspector can render nested
        # sub-rules (a parent row at depth 0 + its depth>0 children) instead of
        # a flat list. ``row_index`` is the step-global document order.
        "depth":             dec.depth,
        "row_index":         dec.row_index,
        "parent_subrule_id": (dec.parent.subrule_id if dec.parent_id else ""),
        "parent_row_index":  (dec.parent.row_index if dec.parent_id else None),
    }


def _custom_rule_dict(raw: dict, *, shape_id: str, sop_id: int,
                      step_number: Any, manual_oos: bool) -> dict[str, Any]:
    """Build one executable rule dict from a canvas-authored custom rule.

    Mirrors the envelope of :func:`_hydrate_decision` so ``execute_shapes`` and
    the evaluator treat it identically to a SOP rule.
    """
    return {
        "key":               raw.get("key"),
        "sop_id":            sop_id,
        "sop_title":         raw.get("sop_title") or "Custom",
        "source":            "decision",
        "section_id":        step_number,
        "section_label":     raw.get("section_label") or "Custom rule",
        "section_category":  "DECISION",
        "yaml_rule_id":      "",
        "subrule_id":        raw.get("subrule_id") or "",
        "step_question":     "",
        "section_narrative": raw.get("section_narrative") or "",
        "condition":         (raw.get("condition") or "").strip(),
        "action":            (raw.get("action") or "").strip(),
        "decision_type":     (raw.get("decision_type") or "").strip(),
        "is_exception":      False,
        "codes":             list(raw.get("codes") or []),
        "is_blocking":       False,
        "step_number":       step_number,
        "is_out_of_scope":   False,
        "is_final":          False,
        "aggregation":       "LEAF",
        "applicable_when":   "",
        "goto_step":         None,
        "binding_id":        "",
        "shape_id":          shape_id,
        "references":        [],
        "excluded_by":       [],
        "manual_oos":        manual_oos,
        "is_custom":         True,
        "depth":             int(raw.get("depth") or 0),
        "parent_key":        raw.get("parent_key"),
        "additional_context": (raw.get("additional_context") or "").strip(),
    }


def _materialise_custom_rules(workflow_id: str,
                              shapes_by_id: dict[str, dict[str, Any]],
                              decisions_out: list[dict[str, Any]]) -> None:
    """Append canvas-authored custom rules (from Shape.properties) as
    executable decision rules, grouped onto their shapes."""
    from builder.models import Shape

    # Host (sop_id, step_number) per shape, taken from its first SOP decision so
    # custom rules execute within the same step/cursor as the node's SOP rules.
    host_by_shape: dict[str, tuple[int, Any]] = {}
    for sg in shapes_by_id.values():
        for rd in sg["rules"]:
            if rd.get("source") == "decision" and rd.get("sop_id"):
                host_by_shape[sg["shape_id"]] = (rd["sop_id"], rd.get("step_number"))
                break

    shapes = (
        Shape.objects
        .filter(workbench__work_area__workflow_id=workflow_id)
        .select_related("workbench")
        .order_by("workbench__order", "order")
    )
    synthetic_step = 9000  # park rule-less manual nodes after real steps
    for shape in shapes:
        props = shape.properties or {}
        customs = [
            r for r in (props.get("sop_rules") or [])
            if isinstance(r, dict)
            and (r.get("is_custom") or str(r.get("key") or "").startswith("custom:"))
        ]
        if not customs:
            continue
        sid = str(shape.id)
        host = host_by_shape.get(sid)
        if host:
            host_sop_id, host_step = host
        else:
            host_sop_id = int(props.get("sop_id") or 0)
            host_step = props.get("step_number")
            if host_step is None:
                synthetic_step += 1
                host_step = synthetic_step
        manual_oos = bool(props.get("manual_out_of_scope"))
        manual_oos_keys = set(props.get("manual_oos_rule_keys") or [])
        manual_in_keys = set(props.get("manual_in_scope_rule_keys") or [])

        sg = shapes_by_id.get(sid)
        if sg is None:
            wb = shape.workbench
            sg = {
                "shape_id":      sid,
                "shape_label":   shape.label or "",
                "workbench":     {"name": wb.name or "", "order": wb.order},
                "shape_order":   shape.order,
                "rules":         [],
                "tool_bindings": [],
            }
            shapes_by_id[sid] = sg

        for raw in customs:
            if not raw.get("key"):
                continue
            _forced_in = raw.get("key") in manual_in_keys
            rd = _custom_rule_dict(
                raw, shape_id=sid, sop_id=host_sop_id,
                step_number=host_step,
                manual_oos=(not _forced_in) and (
                    manual_oos
                    or raw.get("key") in manual_oos_keys
                    or bool(raw.get("manual_out_of_scope"))
                ),
            )
            if _forced_in:
                rd["is_out_of_scope"] = False
            rd["sop_extra_context"] = _workbench_extra_context(shape.workbench)
            decisions_out.append(rd)
            sg["rules"].append(rd)


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
        # Per-SOP auditor context (Workbench.config['extra_context']) — injected
        # into this rule's eval prompt. ``shape__workbench`` is select_related'd.
        rule_dict["sop_extra_context"] = _workbench_extra_context(
            getattr(rb.shape, "workbench", None))
        rule_dict["lob_scope"] = _workbench_lob_scope(
            getattr(rb.shape, "workbench", None))
        rule_dict["references"] = list(rb.references_json or [])
        rule_dict["excluded_by"] = list(rb.excluded_by_json or [])
        # Manual out-of-scope exclusion set by the auditor on the canvas. Two
        # granularities, both honored (the engine skips the affected rule(s)
        # with no LLM call, independent of SOP-derived routing):
        #   • whole node  — ``Shape.properties.manual_out_of_scope`` (every rule)
        #   • per rule     — ``Shape.properties.manual_oos_rule_keys`` contains
        #     this rule's key (covers rules / sub-rules / sub-sub-rules, since
        #     each decision row at any depth has a unique rule_key).
        _shape_props = getattr(rb.shape, "properties", None) or {}
        #   • force IN scope — ``Shape.properties.manual_in_scope_rule_keys``
        #     contains this rule's key. This OVERRIDES the SOP-derived
        #     ``is_out_of_scope`` and any node-level manual OOS, so an
        #     ingestion-flagged rule the auditor re-enabled is evaluated again.
        _forced_in = rb.rule_key in set(
            _shape_props.get("manual_in_scope_rule_keys") or []
        )
        rule_dict["manual_oos"] = (not _forced_in) and (
            bool(_shape_props.get("manual_out_of_scope"))
            or rb.rule_key in set(_shape_props.get("manual_oos_rule_keys") or [])
        )
        if _forced_in:
            rule_dict["is_out_of_scope"] = False
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

    # ── Custom rules authored on the canvas (incl. manual sub-rules) ──────────
    # These live ONLY in ``Shape.properties.sop_rules`` (key ``custom:...``),
    # never as NodeRuleBinding rows (they have no SOP source). Materialise them
    # here as self-contained executable rules so the engine evaluates them like
    # SOP rules. Each inherits the host shape's (sop_id, step_number) so it slots
    # into that step's cursor and is grouped on the same node.
    _materialise_custom_rules(workflow_id, shapes_by_id, decisions_out)

    # Tool binding dicts + scoping lookups
    def _tool_dict(tb) -> dict[str, Any]:
        args = dict(tb.args_template or {})
        # Per-binding LOB scope lives in a reserved args key (``_lob_scope``) so
        # no schema migration is needed; pop it so it is never sent to the tool.
        # Fall back to the built-in default for known Medicare-only tools.
        scope = args.pop("_lob_scope", None)
        if scope is None:
            scope = default_tool_lob_scope(tb.tool.name)
        return {
            "binding_id":      str(tb.id),
            "shape_id":        str(tb.shape_id),
            "rule_binding_id": str(tb.rule_binding_id) if tb.rule_binding_id else "",
            "tool_name":       tb.tool.name,
            "tool_kind":       tb.tool.kind,
            "invoke_url":      tb.tool.invoke_url,
            "args_template":   args,
            "lob_scope":       list(scope or []),
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
