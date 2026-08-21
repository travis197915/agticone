"""Move one workflow's canvas from an old SOP version to a newly-ingested one.

This is what approving an ingestion change set actually *does*. Only the
workflow the SOP was uploaded on is touched — every other workflow keeps
running the version it was already on, which is the whole point of scoping
change sets to a workflow.

A rule's identity on the canvas is its ``NodeRuleBinding.rule_key``, and that
key embeds the SOP's primary key (``step:{sop_id}:{step_number}:{row_index}``).
So a new ingested version invalidates *every* key, not just the changed ones —
which is why the rollout re-pairs the full rule set (``include_unchanged=True``)
rather than working from the reviewed subset alone.

Three things happen to a binding, decided by what the pairing says about it:

matched (modified or unchanged)
    Repointed to the new SOP and the new key.
removed
    Deleted. The rule no longer exists in the document; leaving the binding
    would make the engine load a rule that resolves to nothing.
added
    Nothing — there is no binding to move. New rules are reported as
    *unplaced* and wait for a human to attach them on the canvas. Guessing a
    node is not possible: rules from one step routinely span several nodes
    (SOP 21 spreads step 0 across three), so there is no non-arbitrary answer.

The other half of the job is the binding's ``condition``/``action`` text.
``rule_loader._hydrate_decision`` treats those as *overrides* — when set, they
win over the SOP's own text. They are populated as a copy at attach time on
almost every binding in practice, so repointing the key alone would leave the
engine executing the old wording and make approval a no-op. The rollout
therefore refreshes any binding whose text still matches the old rule, and
leaves alone any binding a human has since edited (reported as ``preserved``).
"""
from __future__ import annotations

import logging
import uuid as uuid_lib
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from django.utils import timezone

from ..models import AuditDecision, AuditPrecondition, AuditSop
from .sop_rule_diff import RuleDelta, diff_sop_versions

logger = logging.getLogger(__name__)

__all__ = [
    "RolloutReport",
    "RolloutPlan",
    "BindingPlan",
    "plan_rollout",
    "preview_rollout",
    "roll_workflow_forward",
]


@dataclass
class RolloutReport:
    """What the rollout did, in terms the reviewer's confirmation can quote."""

    repointed: int = 0
    refreshed: int = 0
    preserved: int = 0
    dropped: int = 0
    stranded: int = 0
    # A binding whose condition/action had been hand-edited, whose source
    # decision the new version removed. Never silently dropped — converted to
    # a workflow-only custom rule on the same shape instead (see
    # ``_orphan_binding_to_custom_rule``), so the auditor's customization
    # survives and is flagged for review rather than lost.
    orphaned: int = 0
    unplaced: list[dict[str, Any]] = field(default_factory=list)
    orphaned_rules: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repointed": self.repointed,
            "refreshed": self.refreshed,
            "preserved": self.preserved,
            "dropped": self.dropped,
            "stranded": self.stranded,
            "orphaned": self.orphaned,
            "unplaced": self.unplaced,
            "unplaced_count": len(self.unplaced),
            "orphaned_rules": self.orphaned_rules,
        }


def _hydrated_condition(decision: AuditDecision) -> str:
    """The condition text as ``rule_loader`` would build it with no override."""
    parts = [p for p in [decision.condition_if, decision.condition_and] if p]
    return " AND ".join(parts).strip()


def _hydrated_action(decision: AuditDecision) -> str:
    return (decision.action_text or decision.action_summary or "").strip()


def _same_text(a: str, b: str) -> bool:
    """Whitespace-insensitive compare.

    Re-ingestion reflows text constantly — a line wrap moving is not a human
    override, and treating it as one would preserve stale wording forever.
    """
    return " ".join((a or "").split()) == " ".join((b or "").split())


def _decision_key(decision: AuditDecision) -> str:
    return f"step:{decision.step.sop_id}:{decision.step.step_number}:{decision.row_index}"


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _precondition_pairs(
    from_sop: AuditSop, to_sop: AuditSop,
) -> dict[tuple[int, int], tuple[int, int, dict]]:
    """Pair precondition *rules* across versions by their condition text.

    Preconditions are a separate key space from decisions and are not part of
    the rule diff. A ``pre:{sop_id}:{pc_id}:{idx}`` key does not name the
    precondition — it names ``llm_rules[idx]`` inside it, so pairing has to be
    at that granularity or a re-ordered list silently rebinds the node to a
    different rule.

    They are rare on real canvases (2 of 593 bindings), so this stays
    deliberately literal: identical condition text means the same rule,
    anything else goes unmatched and gets reported rather than guessed at.
    """
    index: dict[str, tuple[int, int, dict]] = {}
    for pc in AuditPrecondition.objects.filter(sop=to_sop).order_by("display_order", "id"):
        for idx, rule in enumerate(pc.llm_rules or []):
            key = _norm((rule or {}).get("condition") or "")
            if key:
                index.setdefault(key, (pc.id, idx, rule or {}))

    pairs: dict[tuple[int, int], tuple[int, int, dict]] = {}
    for pc in AuditPrecondition.objects.filter(sop=from_sop):
        for idx, rule in enumerate(pc.llm_rules or []):
            match = index.get(_norm((rule or {}).get("condition") or ""))
            if match is not None:
                pairs[(pc.id, idx)] = match
    return pairs


@dataclass
class BindingPlan:
    """What the rollout will do to one existing binding.

    ``condition``/``action`` are the values the binding ends up with, whether
    they were refreshed from the new rule or preserved as an auditor wrote
    them, so a preview can render the final state without re-deriving it.
    """

    binding_id: str
    shape_id: str
    rule_key: str
    action: str
    new_rule_key: str | None = None
    condition: str = ""
    action_text: str = ""
    refreshed: bool = False
    preserved: bool = False
    # Non-overridable fields carried forward from the old decision, used only
    # by the ORPHAN outcome to build the replacement custom-rule dict.
    orphan_meta: dict[str, Any] = field(default_factory=dict)

    REPOINT = "repoint"
    DROP = "drop"
    STRAND = "strand"
    ORPHAN = "orphan"


@dataclass
class RolloutPlan:
    """The complete set of changes, computed without touching anything.

    Planning is separated from applying so the "what happens if I approve
    this?" preview runs the *same* code the approval runs. A preview that
    re-derives the answer separately is a preview that drifts.
    """

    bindings: list[BindingPlan] = field(default_factory=list)
    unplaced: list[dict[str, Any]] = field(default_factory=list)
    report: RolloutReport = field(default_factory=RolloutReport)

    def as_dict(self) -> dict[str, Any]:
        return {
            "report": self.report.as_dict(),
            "bindings": [vars(b) for b in self.bindings],
            "unplaced": self.unplaced,
        }


def _claim(
    claimed: set[tuple[str, str]], shape_id: str, new_key: str, binding, plan,
) -> bool:
    """Reserve ``(shape, new_key)``, or strand the binding if already taken.

    Guards the ``uniq_node_rule_binding_shape_rule`` constraint. A collision
    means two rules on one node collapsed onto the same ambiguous key, which is
    a defect in the key scheme rather than in this rule — so the binding is left
    on the old version and counted, never dropped.
    """
    if (shape_id, new_key) in claimed:
        plan.report.stranded += 1
        logger.warning(
            "rollout: binding %s would collide with an existing %s on shape %s "
            "(rule_key is not unique across nesting depth) — left in place",
            binding.rule_key, new_key, shape_id,
        )
        return False
    claimed.add((shape_id, new_key))
    return True


def plan_rollout(
    *,
    bindings: list,
    from_sop: AuditSop,
    to_sop: AuditSop,
    use_embeddings: bool = True,
) -> RolloutPlan:
    """Decide what happens to each binding. Reads only — writes nothing."""
    plan = RolloutPlan()
    if not bindings:
        return plan

    deltas = diff_sop_versions(
        from_sop, to_sop,
        use_embeddings=use_embeddings,
        include_unchanged=True,
    )

    # old rule_key -> the decision that replaces it (None means it was removed)
    replacement: dict[str, AuditDecision | None] = {}
    previous: dict[str, AuditDecision] = {}
    for delta in deltas:
        if delta.from_decision is None:
            continue  # an addition has no old key to map from
        key = _decision_key(delta.from_decision)
        previous[key] = delta.from_decision
        replacement[key] = delta.to_decision  # None for REMOVED

    pre_pairs = _precondition_pairs(from_sop, to_sop)

    # ``rule_key`` is ``step:{sop}:{step_number}:{row_index}`` and is NOT unique
    # per decision — nested subrules reuse ``row_index`` at a deeper ``depth``,
    # so one key can name five decisions in the same step. That ambiguity is
    # baked into the key scheme (``rule_loader`` builds it the same way), not
    # something the rollout can fix here.
    #
    # It matters because ``NodeRuleBinding`` is unique on ``(shape, rule_key)``:
    # two bindings on one shape whose old keys differ can resolve to the SAME
    # new key, and repointing both raises IntegrityError — approval dies
    # halfway. Claim keys as they are assigned and strand the loser instead, so
    # a re-parse of a nested table degrades one rule rather than the batch.
    claimed: set[tuple[str, str]] = set()

    for binding in bindings:
        entry = BindingPlan(
            binding_id=str(binding.id),
            shape_id=str(binding.shape_id),
            rule_key=binding.rule_key,
            action=BindingPlan.STRAND,
            condition=binding.condition,
            action_text=binding.action,
        )
        kind = (binding.rule_key.split(":") or [""])[0]

        if kind == "pre":
            matched = _rolled_precondition(binding, to_sop, pre_pairs)
            if matched is None:
                # No counterpart in the new document. Dropping it silently
                # would quietly shrink the audit; report it instead.
                plan.report.stranded += 1
                logger.warning(
                    "rollout: precondition binding %s has no match in sop=%s",
                    binding.rule_key, to_sop.id,
                )
                plan.bindings.append(entry)
                continue
            new_key, new_rule = matched
            if not _claim(claimed, entry.shape_id, new_key, binding, plan):
                plan.bindings.append(entry)
                continue
            entry.action = BindingPlan.REPOINT
            entry.new_rule_key = new_key
            # Same override rule as decisions: refresh a copy, keep an edit.
            # The condition is what paired them, so only the action can differ.
            new_action = (new_rule.get("action") or "").strip()
            if binding.action and not _same_text(binding.action, new_action):
                entry.preserved = True
                plan.report.preserved += 1
            elif new_action != binding.action:
                entry.action_text = new_action
                entry.refreshed = True
                plan.report.refreshed += 1
            plan.report.repointed += 1
            plan.bindings.append(entry)
            continue

        if binding.rule_key not in replacement:
            # A step binding the matcher never saw — its decision row is gone
            # from the old SOP, or the key predates a renumbering. Left in
            # place on the old SOP rather than deleted: it is not ours to
            # decide, and a stale-but-present rule is recoverable.
            plan.report.stranded += 1
            logger.warning(
                "rollout: binding %s not present in the %s->%s pairing",
                binding.rule_key, from_sop.id, to_sop.id,
            )
            plan.bindings.append(entry)
            continue

        new_decision = replacement[binding.rule_key]
        if new_decision is None:
            old_decision = previous[binding.rule_key]
            # A binding copied at attach time and never touched matches the
            # old decision's hydrated text exactly — safe to drop, same as
            # today. One that diverges was hand-edited: the source rule is
            # gone, but the auditor's customization is not ours to discard.
            edited = (
                not _same_text(binding.condition, _hydrated_condition(old_decision))
                or not _same_text(binding.action, _hydrated_action(old_decision))
            )
            if edited:
                entry.action = BindingPlan.ORPHAN
                entry.condition = binding.condition
                entry.action_text = binding.action
                entry.orphan_meta = {
                    "decision_type": old_decision.decision_type or "",
                    "subrule_id": old_decision.subrule_id or "",
                    "codes": list(old_decision.all_codes or []),
                    "step_number": old_decision.step.step_number,
                    "row_index": old_decision.row_index,
                }
                plan.report.orphaned += 1
                plan.report.orphaned_rules.append({
                    "rule_key": binding.rule_key,
                    "shape_id": entry.shape_id,
                    "step_number": old_decision.step.step_number,
                    "row_index": old_decision.row_index,
                    "subrule_id": old_decision.subrule_id or "",
                    "condition": binding.condition,
                    "action": binding.action,
                })
            else:
                entry.action = BindingPlan.DROP
                plan.report.dropped += 1
            plan.bindings.append(entry)
            continue

        old_decision = previous[binding.rule_key]
        new_key = _decision_key(new_decision)
        if not _claim(claimed, entry.shape_id, new_key, binding, plan):
            plan.bindings.append(entry)
            continue
        entry.action = BindingPlan.REPOINT
        entry.new_rule_key = new_key

        # Refresh a copied override; keep a hand-edited one. The two fields are
        # judged independently, and a binding can land in both buckets — an
        # auditor who rewrote only the action still needs the condition moved,
        # and the reviewer still needs telling that their action survived and
        # may now contradict the new document.
        for attr, hydrate in (("condition", _hydrated_condition),
                              ("action_text", _hydrated_action)):
            current = getattr(entry, attr)
            if _same_text(current, hydrate(old_decision)):
                setattr(entry, attr, hydrate(new_decision))
                entry.refreshed = True
            elif current:
                entry.preserved = True

        plan.report.repointed += 1
        plan.report.refreshed += int(entry.refreshed)
        plan.report.preserved += int(entry.preserved)
        plan.bindings.append(entry)

    for delta in deltas:
        if delta.kind != RuleDelta.ADDED:
            continue
        decision = delta.to_decision
        plan.unplaced.append({
            "rule_key": _decision_key(decision),
            "step_number": decision.step.step_number,
            "row_index": decision.row_index,
            "subrule_id": decision.subrule_id or "",
            "condition": _hydrated_condition(decision),
            "action": _hydrated_action(decision),
        })
    plan.report.unplaced = plan.unplaced
    return plan


def preview_rollout(
    *,
    workflow,
    from_sop: AuditSop,
    to_sop: AuditSop,
    use_embeddings: bool = True,
) -> RolloutPlan:
    """The plan for a workflow, without locking or writing anything."""
    from agent_tools.models import NodeRuleBinding

    bindings = list(
        NodeRuleBinding.objects
        .filter(shape__workbench__work_area__workflow=workflow, sop=from_sop)
    )
    return plan_rollout(
        bindings=bindings, from_sop=from_sop, to_sop=to_sop,
        use_embeddings=use_embeddings,
    )


@transaction.atomic
def roll_workflow_forward(
    *,
    workflow,
    from_sop: AuditSop,
    to_sop: AuditSop,
    use_embeddings: bool = True,
) -> RolloutReport:
    """Repoint ``workflow``'s rule bindings from ``from_sop`` to ``to_sop``.

    Plans first, then applies — the plan is the same one the preview renders,
    so what a reviewer was shown is what they get.

    Returns a :class:`RolloutReport`. Safe to call when the workflow has no
    bindings on ``from_sop`` — the report simply comes back empty.
    """
    from agent_tools.models import NodeRuleBinding

    bindings = list(
        NodeRuleBinding.objects
        .select_for_update()
        .filter(shape__workbench__work_area__workflow=workflow, sop=from_sop)
    )
    if not bindings:
        logger.info(
            "rollout: workflow=%s has no bindings on sop=%s — nothing to move",
            getattr(workflow, "id", None), from_sop.id,
        )
        return RolloutReport()

    plan = plan_rollout(
        bindings=bindings, from_sop=from_sop, to_sop=to_sop,
        use_embeddings=use_embeddings,
    )
    by_id = {str(b.id): b for b in bindings}
    # One Shape can carry several of this rollout's bindings, so fetch/mutate
    # each touched shape once (not once per binding) and save once at the
    # end — see _sync_shape_rule_entry.
    shapes_cache: dict[str, Any] = {}
    dirty_shape_ids: set[str] = set()

    for entry in plan.bindings:
        binding = by_id.get(entry.binding_id)
        if binding is None:  # pragma: no cover - defensive
            continue
        if entry.action == BindingPlan.STRAND:
            continue
        if entry.action == BindingPlan.DROP:
            binding.delete()
            continue
        if entry.action == BindingPlan.ORPHAN:
            _orphan_binding_to_custom_rule(binding, entry)
            continue

        old_rule_key = binding.rule_key
        binding.sop = to_sop
        binding.rule_key = entry.new_rule_key or binding.rule_key
        updates = ["sop", "rule_key", "updated_at"]
        if binding.condition != entry.condition:
            binding.condition = entry.condition
            updates.append("condition")
        if binding.action != entry.action_text:
            binding.action = entry.action_text
            updates.append("action")
        binding.save(update_fields=updates)

        _sync_shape_rule_entry(
            shapes_cache, dirty_shape_ids, entry.shape_id, old_rule_key, binding,
        )

    for shape_id in dirty_shape_ids:
        shape = shapes_cache[shape_id]
        shape.save(update_fields=["properties", "updated_at"])

    logger.info(
        "rollout: workflow=%s sop %s->%s %s",
        getattr(workflow, "id", None), from_sop.id, to_sop.id,
        plan.report.as_dict(),
    )
    return plan.report


def _sync_shape_rule_entry(
    shapes_cache: dict[str, Any],
    dirty_shape_ids: set[str],
    shape_id: str,
    old_rule_key: str,
    binding,
) -> None:
    """Keep one Shape.properties.sop_rules[] entry in step with its binding
    after a repoint.

    ``NodeRuleBinding`` is the source of truth the engine executes, but the
    canvas keeps a second copy of the same rule in the shape's raw JSON — the
    only place organizational fields NodeRuleBinding doesn't have (sop_title,
    decision_type, codes, subrule_id, manual scope toggles, ...) live. Left
    untouched after a repoint, that copy still carries the OLD
    key/sop_id/condition/action, and anything that reads shape.properties raw
    (``approve_canvas_change_set``, ``WorkflowGraphWriter``) then silently
    disagrees with the binding table — the exact failure this closes.

    Only the four fields the binding is authoritative for are patched, and
    only by copying whatever the rollout already decided onto the binding
    (refreshed to the new text, or preserved because an auditor hand-edited
    it — that decision already happened in ``roll_workflow_forward``'s caller
    and is reflected in ``binding.condition``/``binding.action`` by the time
    this runs). Every other field on the entry — organizational metadata, an
    auditor's manual scope toggle — is left exactly as it was.
    """
    from builder.models import Shape

    shape_id = str(shape_id)
    shape = shapes_cache.get(shape_id)
    if shape is None:
        shape = Shape.objects.select_for_update().filter(pk=shape_id).first()
        if shape is None:  # pragma: no cover - defensive
            return
        shapes_cache[shape_id] = shape

    props = shape.properties
    rules = props.get("sop_rules") if isinstance(props, dict) else None
    if not isinstance(rules, list):
        return
    for rule in rules:
        if isinstance(rule, dict) and rule.get("key") == old_rule_key:
            rule["key"] = binding.rule_key
            rule["sop_id"] = binding.sop_id
            rule["condition"] = binding.condition
            rule["action"] = binding.action
            dirty_shape_ids.add(shape_id)
            break


def _orphan_binding_to_custom_rule(binding, entry: "BindingPlan") -> None:
    """Convert a hand-edited binding whose source decision was removed into a
    workflow-only custom rule on the same shape, then delete the binding.

    There is nothing left in the new document to repoint to, but the
    auditor's edited text is not ours to discard — it is preserved verbatim
    as a ``custom:`` rule (never a ``NodeRuleBinding``, since it has no SOP
    to point at), tagged with provenance so the UI can flag it for review
    instead of rendering identically to a deliberately-authored custom rule.
    """
    from builder.models import Shape

    shape = Shape.objects.select_for_update().get(pk=binding.shape_id)
    props = dict(shape.properties or {})
    rules = list(props.get("sop_rules") or [])
    meta = entry.orphan_meta or {}
    rules.append({
        "key": f"custom:{uuid_lib.uuid4()}",
        "sop_id": 0,
        "sop_title": "Custom",
        "is_custom": True,
        "condition": entry.condition,
        "action": entry.action_text,
        "decision_type": meta.get("decision_type", ""),
        "subrule_id": meta.get("subrule_id", ""),
        "codes": meta.get("codes", []),
        "orphaned_from_rule_key": entry.rule_key,
        "orphaned_from_sop_id": binding.sop_id,
        "orphaned_reason": "sop_rule_removed_in_new_version",
        "orphaned_at": timezone.now().isoformat(),
    })
    props["sop_rules"] = rules
    shape.properties = props
    shape.save(update_fields=["properties", "updated_at"])
    binding.delete()
    logger.info(
        "rollout: orphaned binding %s (shape=%s) converted to custom rule — "
        "source decision removed in new SOP version",
        entry.rule_key, entry.shape_id,
    )


def _rolled_precondition(binding, to_sop: AuditSop, pre_pairs):
    """``(new pre: key, the matched rule dict)`` for a binding, or None."""
    parts = binding.rule_key.split(":")
    if len(parts) < 4:
        return None
    try:
        old_pc_id, old_idx = int(parts[2]), int(parts[3])
    except (TypeError, ValueError):
        return None
    match = pre_pairs.get((old_pc_id, old_idx))
    if match is None:
        return None
    new_pc_id, new_idx, rule = match
    return f"pre:{to_sop.id}:{new_pc_id}:{new_idx}", rule
