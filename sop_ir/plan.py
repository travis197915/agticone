"""Pure planning pass over a :class:`~sop_ir.schema.SopIR` (no Django, no DB).

This is the routing brain shared by both ingestion doors: it turns the IR rule
tree into the intermediate ``step``/``decision`` dicts that
:func:`sop_ir.persist.persist_ir` writes as ``AuditStep`` / ``AuditDecision``
rows. Kept Django-free so parity/roundtrip tests (and the standalone pipeline)
can plan without a configured Django app.

All heuristics (disposition classification, code extraction, goto inference,
aggregation, OOS) come from :mod:`sop_ir.normalize`.
"""
from __future__ import annotations

import re
from typing import Optional

from .normalize import (
    classify_decision,
    extract_codes,
    extract_goto,
    infer_aggregation,
    is_out_of_scope,
    map_aggregation_rule,
)
from .schema import Navigation, RuleNode, SopIR, Subrule


def _resolve_goto(*texts: str, nav: Optional[Navigation]) -> Optional[int]:
    """Prefer a structured Navigation hint, else parse it out of free text.

    For the YAML door ``nav`` is always None, so behaviour is identical to the
    legacy ``_extract_goto`` call. For the HTML/PDF door the maker can emit a
    structured ``{"op": "goto", "step_number": N}`` and we honour it."""
    if nav is not None and nav.op.value == "goto" and nav.step_number is not None:
        return nav.step_number
    return extract_goto(" ".join(t for t in texts if t))


def _plan_rule(rule: RuleNode, ridx: int) -> dict:
    rule_id = rule.rule_id
    step_number = rule.step_number if rule.step_number is not None else ridx

    description = rule.description
    conditions = list(rule.conditions)
    actions = list(rule.actions)
    output = rule.output
    subrules = rule.subrules

    intro_bits = []
    if conditions:
        intro_bits.append("Conditions:\n- " + "\n- ".join(conditions))
    if actions:
        intro_bits.append("Actions:\n- " + "\n- ".join(actions))
    if output:
        intro_bits.append("Output (Met/Not-Met):\n" + output)
    intro_text = "\n\n".join(intro_bits)

    step_oos = bool(getattr(rule, "is_out_of_scope", False)) or \
        is_out_of_scope(description, conditions, actions, output)
    # A "blank" step has no real evaluation criteria. When out of scope it must
    # be SKIPPED and the audit must CONTINUE (non-final), not treated as a
    # terminal exclusion that halts the path.
    is_blank = not conditions and not actions and not subrules
    action_blob = " ".join([description] + actions + [output])
    terminal_action = ""
    is_terminal = False
    m = re.search(r"\(?(F[3-5])\)?\b", (description + " " + " ".join(actions)).upper())
    if m and ("process the claim" in action_blob.lower()
              or "save the claim" in action_blob.lower()):
        is_terminal = True
        terminal_action = m.group(1)

    children = [
        _plan_subrule(sr, i, depth=0, parent_oos=step_oos)
        for i, sr in enumerate(subrules)
    ]

    # Explicit step-level routing of the child group: an `aggregation_rule`
    # YAML key — or the mere presence of `applicable_when` on a direct child —
    # forces APPLICABLE_ONLY so the engine evaluates only the applicable child.
    agg_rule = map_aggregation_rule(rule.aggregation_rule)
    if children:
        any_applicable_when = any(c.get("applicable_when") for c in children)
        forced = "APPLICABLE_ONLY" if (agg_rule == "APPLICABLE_ONLY" or any_applicable_when) else agg_rule
        if forced:
            for c in children:
                c["aggregation"] = forced

    # Leaf top-level rule (no subrules): synthesize ONE decision row so the rule
    # body (codes / output / decision_type) is captured and rendered.
    if not children:
        codes = extract_codes(action_blob)
        children = [{
            "subrule_id": rule_id,
            "table_name": rule.section,
            "depth": 0,
            "row_index": 0,
            "condition_if": "\n".join(conditions) if conditions else "(applies to this step)",
            "condition_and": "",
            "action_text": "\n".join(actions) if actions else description,
            "output_text": output,
            "decision_type": classify_decision(action_blob),
            "tooling_allowed": bool(rule.tooling_allowed),
            "is_out_of_scope": step_oos,
            "goto_step": _resolve_goto(action_blob, nav=rule.navigation),
            "is_final": is_terminal or (step_oos and not is_blank),
            "aggregation": "LEAF",
            "codes": codes,
            "children": [],
            "_synthetic": True,
        }]

    return {
        "rule_id": rule_id,
        "step_number": step_number,
        "question": description,
        "intro_text": intro_text,
        "is_out_of_scope": step_oos,
        "is_terminal": is_terminal,
        "terminal_action": terminal_action,
        "references": list(rule.references),
        "urls": list(rule.urls),
        "children": children,
    }


def _plan_subrule(sr: Subrule, idx: int, depth: int, parent_oos: bool) -> dict:
    subrule_id = sr.subrule_id
    description = sr.description
    conditions = list(sr.conditions)
    actions = list(sr.actions)
    output = sr.output
    sub = sr.subrules

    own_oos = bool(getattr(sr, "is_out_of_scope", False)) or \
        is_out_of_scope(description, conditions, actions, output)
    oos = own_oos or parent_oos

    condition_if = description or (conditions[0] if conditions else "")
    condition_and = "\n".join(conditions)
    action_text = "\n".join(actions)

    codes = extract_codes(" ".join([description] + conditions + actions + [output]))
    has_children = bool(sub)

    children = [
        _plan_subrule(s, i, depth=depth + 1, parent_oos=oos)
        for i, s in enumerate(sub)
    ]

    return {
        "subrule_id": subrule_id,
        "table_name": sr.table_name,
        "depth": depth,
        "row_index": idx,
        "condition_if": condition_if,
        "condition_and": condition_and,
        "action_text": action_text,
        "output_text": output,
        "applicable_when": sr.applicable_when,
        "decision_type": classify_decision(" ".join(actions) + " " + description + " " + output),
        "tooling_allowed": bool(sr.tooling_allowed),
        "is_out_of_scope": oos,
        "goto_step": _resolve_goto(" ".join(actions), output, nav=sr.navigation),
        "is_final": oos or bool(extract_goto(" ".join(actions)) is None and "stop" in action_text.lower()),
        "aggregation": infer_aggregation(output, has_children),
        "codes": codes,
        "children": children,
        "urls": list(sr.urls),
    }


def plan_ir(ir: SopIR) -> list[dict]:
    """Pure planning pass (no DB) — used by persist_ir, --dry-run and tests."""
    return [_plan_rule(r, i) for i, r in enumerate(ir.rules)]
