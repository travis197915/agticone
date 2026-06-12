"""Routing-invariant validation for a :class:`~sop_ir.schema.SopIR`.

The execution engine's step cursor routes off ``goto_step`` /
``applicable_when`` / out-of-scope flags. If those are inconsistent the engine
silently falls back to a flat sequential walk — exactly the bug the HTML/PDF
door has today. ``validate_ir`` catches the inconsistencies *before* the IR is
persisted so the maker/checker stage can repair them.

Contract::

    ok, errors = validate_ir(ir)

``errors`` is a list of human-readable strings, each prefixed ``ERROR:`` or
``WARN:``. ``ok`` is True iff there are no ``ERROR:``-level findings (warnings
are advisory and do not block persistence).
"""
from __future__ import annotations

from typing import List, Tuple

from .normalize import extract_goto, map_aggregation_rule
from .schema import RuleNode, SopIR, Subrule


def _goto_target(*texts: str, nav) -> int | None:
    """Resolve a goto target from a structured Navigation or free text."""
    if nav is not None and nav.step_number is not None and nav.op.value == "goto":
        return nav.step_number
    return extract_goto(" ".join(t for t in texts if t))


def _walk_subrules(sr: Subrule, depth: int, errors: List[str], declared: set,
                   rule_id: str) -> None:
    label = sr.subrule_id or f"{rule_id}/subrule@d{depth}"

    # Nested subrules really ought to carry a stable id (the row key the engine
    # and builder /attachable/ endpoint rely on for nested rules).
    if depth > 0 and not sr.subrule_id:
        errors.append(f"WARN: subrule under {rule_id} at depth {depth} has no subrule_id")

    target = _goto_target(" ".join(sr.actions), sr.output, nav=sr.navigation)
    if target is not None and target not in declared:
        errors.append(
            f"ERROR: {label} routes to step {target} which is not a declared step_number")

    for child in sr.subrules:
        _walk_subrules(child, depth + 1, errors, declared, rule_id)


def _check_applicable_when(rule: RuleNode, errors: List[str]) -> None:
    any_applicable = any(c.applicable_when for c in rule.subrules)
    declared_applicable = map_aggregation_rule(rule.aggregation_rule) == "APPLICABLE_ONLY"
    if declared_applicable and rule.subrules and not any_applicable:
        errors.append(
            f"WARN: {rule.rule_id} declares applicable_only aggregation but no "
            f"subrule sets applicable_when")
    # Note: the reverse (applicable_when present without aggregation_rule) is
    # auto-healed by persist_ir, which forces APPLICABLE_ONLY in that case.


def validate_ir(ir: SopIR) -> Tuple[bool, List[str]]:
    errors: List[str] = []

    if not ir.rules:
        errors.append("ERROR: SOP IR has no rules")
        return False, errors

    # Declared step numbers (with source-order fallback, matching persist_ir).
    declared: set = set(ir.step_numbers())

    # Duplicate explicit step_number is a routing hazard (two AuditSteps would
    # collide on the (sop, step_number) unique constraint).
    seen: dict[int, str] = {}
    for r in ir.rules:
        if r.step_number is None:
            continue
        if r.step_number in seen:
            errors.append(
                f"ERROR: step_number {r.step_number} used by both "
                f"{seen[r.step_number]} and {r.rule_id}")
        else:
            seen[r.step_number] = r.rule_id or f"step{r.step_number}"

    for r in ir.rules:
        # Top-level rule routing (its own navigation / output).
        target = _goto_target(" ".join(r.actions), r.output, nav=r.navigation)
        if target is not None and target not in declared:
            errors.append(
                f"ERROR: {r.rule_id or 'rule'} routes to step {target} which is "
                f"not a declared step_number")
        _check_applicable_when(r, errors)
        for sr in r.subrules:
            _walk_subrules(sr, 0, errors, declared, r.rule_id or "rule")

    ok = not any(e.startswith("ERROR:") for e in errors)
    return ok, errors
