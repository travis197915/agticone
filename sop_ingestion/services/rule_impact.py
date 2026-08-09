"""What else a rule edit touches — the review modal's "Affects" strip.

Two independent dimensions, both resolved here:

**Within the SOP** — the steps a rule routes to. ``AuditDecision.goto_step``
holds only the *primary* branch, but a routing rule commonly names two:

    "If received within timely filing — Override … and skip to Step 15.
     If not received within timely filing — Skip to Step 6."

``goto_step`` on that row is ``15``; Step 6 lives only in the prose. Taking the
union of both is what reproduces the reference modal's
"Affects Step 6 · …, Step 15 · …" for a single rule.

**Across workflows** — which builder workflows bind this exact rule. Edits land
on ``AuditDecision`` (the SOP source of truth), so an approved change reaches
every workflow bound to that rule, not just the one the author had in mind.
Surfacing it is the only guardrail the reviewer gets.

Only *direct* routing targets are reported. Following the graph transitively
would soon name most of the SOP and stop meaning anything.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable, Sequence

from ..models import AuditDecision, AuditStep

__all__ = [
    "parse_routed_step_numbers",
    "dependent_steps_for_decisions",
    "affected_workflows_for_rule_keys",
    "resolve_for_proposals",
]

# "Skip to Step 15", "proceed to step 6", "Go to Step 9".
_STEP_MENTION_RE = re.compile(r"\bstep\s+(\d{1,3})\b", re.IGNORECASE)

_LABEL_MAX = 80


def parse_routed_step_numbers(decision: AuditDecision) -> set[int]:
    """Step numbers this decision routes to, from both structured and prose.

    Excludes the decision's own step — a rule referring to itself is not a
    dependency, and self-references are common in narrative action text.
    """
    targets: set[int] = set()
    if decision.goto_step:
        try:
            targets.add(int(decision.goto_step))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            pass
    for match in _STEP_MENTION_RE.finditer(decision.action_text or ""):
        try:
            targets.add(int(match.group(1)))
        except ValueError:  # pragma: no cover - regex guarantees digits
            continue
    targets.discard(getattr(decision.step, "step_number", None))
    return {n for n in targets if n is not None and n >= 0}


def _step_label(step: AuditStep) -> str:
    """Human label for the Affects strip: 'Step 6 · Does the claim include…'."""
    raw = (step.question or "").strip() or (step.terminal_action or "").strip()
    if not raw:
        raw = (step.sub_procedure_name or "").strip()
    label = " ".join(raw.split())
    return f"{label[:_LABEL_MAX]}…" if len(label) > _LABEL_MAX else label


def dependent_steps_for_decisions(
    decisions: Sequence[AuditDecision],
) -> dict[int, list[dict[str, Any]]]:
    """``{decision_id: [{step_number, label}, ...]}``.

    Batched: one query resolves labels for every referenced step across all the
    decisions passed in, however many change sets they span.
    """
    if not decisions:
        return {}

    wanted: dict[int, set[int]] = {}
    by_sop: dict[int, set[int]] = defaultdict(set)
    for decision in decisions:
        step = decision.step
        numbers = parse_routed_step_numbers(decision)
        wanted[decision.id] = numbers
        if numbers:
            by_sop[step.sop_id].update(numbers)

    labels: dict[tuple[int, int], str] = {}
    for sop_id, numbers in by_sop.items():
        rows = AuditStep.objects.filter(sop_id=sop_id, step_number__in=numbers)
        for step in rows:
            labels[(sop_id, step.step_number)] = _step_label(step)

    out: dict[int, list[dict[str, Any]]] = {}
    for decision in decisions:
        sop_id = decision.step.sop_id
        entries = []
        for number in sorted(wanted.get(decision.id, ())):
            label = labels.get((sop_id, number))
            if label is None:
                # A prose mention that names a step this SOP does not have —
                # a cross-SOP reference or a typo. Dropped rather than shown
                # as a dead link.
                continue
            entries.append({"step_number": number, "label": label})
        out[decision.id] = entries
    return out


def _safe_node_rule_binding():
    try:
        from agent_tools.models import NodeRuleBinding
    except Exception:  # pragma: no cover - agent_tools optional at import time
        return None
    return NodeRuleBinding


def affected_workflows_for_rule_keys(
    rule_keys: Iterable[str],
) -> dict[str, list[dict[str, Any]]]:
    """``{rule_key: [{id, name}, ...]}`` — workflows binding each rule.

    One query for the whole batch. Reported per rule rather than per SOP: a
    workflow may bind only some of a SOP's rules, and the reviewer cares about
    the rules actually being changed.
    """
    keys = [k for k in rule_keys if k]
    NodeRuleBinding = _safe_node_rule_binding()
    if NodeRuleBinding is None or not keys:
        return {}

    rows = (
        NodeRuleBinding.objects
        .filter(rule_key__in=keys)
        .values_list(
            "rule_key",
            "shape__workbench__work_area__workflow_id",
            "shape__workbench__work_area__workflow__name",
        )
        .distinct()
    )

    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for rule_key, workflow_id, name in rows:
        if not workflow_id:
            continue
        wid = str(workflow_id)
        if (rule_key, wid) in seen:
            continue
        seen.add((rule_key, wid))
        out[rule_key].append({"id": wid, "name": name or ""})

    for entries in out.values():
        entries.sort(key=lambda e: (e["name"] or "", e["id"]))
    return dict(out)


def resolve_for_proposals(proposals: Sequence[Any]) -> dict[int, dict[str, Any]]:
    """``{proposal_id: {dependent_steps, affected_workflows}}``.

    Resolved at read time as well as at propose time — the builder graph can be
    rewired after an edit is proposed, and a stale blast radius is worse than
    none.

    Callers should pass proposals whose ``decision`` and ``decision.step`` are
    already selected; this adds two queries total regardless of batch size.

    A proposal from a re-ingestion may have no ``decision`` at all — an *added*
    rule exists only on the new SOP. Its routing is read from the incoming
    ``to_decision`` instead, which is the right side to read anyway: the whole
    question is where this new rule will send the audit once it lands.
    """
    from .rule_changes import _rule_key  # local import avoids a cycle

    if not proposals:
        return {}

    # The side of each proposal that carries the routing, and the id to key the
    # answer by (``decision_id`` is None for an addition).
    anchors = {p.id: (p.decision or getattr(p, "to_decision", None))
               for p in proposals}
    steps_by_decision = dependent_steps_for_decisions(
        [d for d in anchors.values() if d is not None]
    )

    rule_keys = {}
    for p in proposals:
        anchor = anchors[p.id]
        if anchor is None:
            continue  # a removal on a batch with no target SOP — nothing to key
        rule_keys[p.id] = _rule_key(
            anchor.step.sop_id, p.step_number, p.row_index
        )
    workflows_by_key = affected_workflows_for_rule_keys(set(rule_keys.values()))

    return {
        p.id: {
            "dependent_steps": (
                steps_by_decision.get(anchors[p.id].id, [])
                if anchors[p.id] is not None else []
            ),
            "affected_workflows": workflows_by_key.get(rule_keys.get(p.id), []),
        }
        for p in proposals
    }
