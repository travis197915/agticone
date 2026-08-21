"""Pending-change review for canvas rule edits (add/edit/delete a rule).

Mirrors ``sop_ingestion.services.rule_changes`` (auditor edits an
``AuditDecision`` directly) but targets a builder ``Shape`` + ``rule_key``
instead — the only target custom rules have, and the one canvas-level
SOP-derived rule edits use too (the canonical ``AuditDecision`` is never
touched from here). Both flows share the same ``RuleChangeSet``/
``RuleChangeProposal`` tables and the same review endpoints
(``sop_ingestion.rule_change_views``); this module is dispatched to from
``sop_ingestion.services.rule_changes.approve_change_set``/``change_set_payload``
whenever ``change_set.source == ChangeSetSource.CANVAS``.

A canvas edit never writes ``Shape.properties`` or ``NodeRuleBinding`` at
propose time — only at approve time, via :func:`approve_canvas_change_set`,
which is also the sole place a canvas rule edit creates a new
``WorkflowVersion``/``WorkflowVersionRule`` snapshot (via
``builder.workflow_versioning.snapshot_workflow_version``). Reject never
applies anything and never versions — see
``sop_ingestion.services.rule_changes.reject_change_set``, which already
handles CANVAS batches correctly with no special-casing (it only has a
side effect to undo for INGESTION batches).
"""
from __future__ import annotations

import re
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from sop_ingestion.models import (
    ChangeSetSource,
    ChangeSetStatus,
    RuleChangeKind,
    RuleChangeProposal,
    RuleChangeSet,
)
from sop_ingestion.rule_reconcile import _VALID_DECISION_TYPES
from sop_ingestion.services.rule_changes import (
    NoEffectiveChange,
    RuleChangeError,
)

from .bindings_sync import (
    _rule_sop_id,
    _resolve_sop,
    extract_bindings_from_properties,
    hydrate_properties_with_bindings,
)
from .models import Shape, Workflow

# The auditor-editable fields for a canvas rule-content EDIT — same
# reconcilable scope as sop_ingestion.rule_reconcile.RECONCILABLE_FIELDS,
# adapted to the shape.properties.sop_rules[] entry shape. Scope-override
# toggles (manual_oos_rule_keys / manual_in_scope_rule_keys) are a separate,
# coarser mechanism and are deliberately out of scope here.
CANVAS_RULE_FIELDS = ("condition", "action", "decision_type", "codes", "subrule_id")

# A brand-new custom rule is created wholesale — there is no "previous" rule
# to diff an edit against — so ADD accepts a broader field set than an EDIT's
# diff does: the organizational/placement fields the custom-rule form sets
# alongside condition/action (section_label, additional_context for the
# node's inspector; parent_key/depth for sub-rule nesting) that a plain
# condition/action/decision_type edit never touches. These are intentionally
# NOT part of workflow_rule_fingerprint's tracked fields (see
# builder.bindings_sync._RULE_FINGERPRINT_SCALAR_FIELDS) — they're
# organizational, not execution-meaningful — but must still be captured when
# the rule is FIRST created, since there is no later path to set them.
CANVAS_RULE_ADD_FIELDS = CANVAS_RULE_FIELDS + (
    "section_label", "additional_context", "parent_key", "depth", "sop_title", "source",
)

__all__ = [
    "CANVAS_RULE_FIELDS",
    "propose_canvas_rule_change",
    "approve_canvas_change_set",
    "canvas_change_set_payload",
]


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def _jsonable(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if isinstance(value, (bool, int, float, str)) or value is None:
            out[key] = value
        elif isinstance(value, list):
            out[key] = list(value)
        else:
            out[key] = str(value)
    return out


def _sanitize_canvas_fields(proposed: dict, current: dict) -> dict[str, Any]:
    """Keep only whitelisted fields whose value actually differs + is valid."""
    clean: dict[str, Any] = {}
    if not isinstance(proposed, dict):
        return clean
    for f in CANVAS_RULE_FIELDS:
        if f not in proposed:
            continue
        val = proposed[f]
        if f == "decision_type":
            v = str(val or "").upper().strip()
            if v in _VALID_DECISION_TYPES and v != (current.get(f) or ""):
                clean[f] = v
        elif f == "codes":
            v = list(val) if isinstance(val, list) else []
            if v != list(current.get(f) or []):
                clean[f] = v
        else:
            v = str(val if val is not None else "")
            if _norm(v) != _norm(current.get(f)):
                clean[f] = v
    return clean


def _current_rule_entry(shape, rule_key: str) -> dict[str, Any] | None:
    """The rule's current, live state — read the same way the canvas GET
    would see it (bound fields from NodeRuleBinding when present, raw JSON
    fields like decision_type/codes/subrule_id always)."""
    props = hydrate_properties_with_bindings(shape)
    for rule in props.get("sop_rules") or []:
        if isinstance(rule, dict) and rule.get("key") == rule_key:
            return rule
    return None


# ── Propose ──────────────────────────────────────────────────────────────────


@transaction.atomic
def propose_canvas_rule_change(
    *,
    workflow: Workflow,
    shape: Shape,
    rule_key: str,
    kind: str,
    fields: dict[str, Any],
    is_custom: bool,
    author: str,
) -> RuleChangeProposal:
    """Record a proposed add/edit/delete of one canvas rule.

    Does not touch ``Shape.properties`` or ``NodeRuleBinding`` — the change
    only lands in a ``RuleChangeProposal`` and waits for review.
    """
    author = (author or "").strip()[:128]
    kind = kind if kind in RuleChangeKind.values else RuleChangeKind.MODIFIED

    # Re-read rather than trusting the caller's instance, same reasoning as
    # sop_ingestion.services.rule_changes.propose_rule_change: a shape fetched
    # before another author's batch was approved carries stale rule content.
    workflow = Workflow.objects.get(pk=workflow.pk)
    shape = Shape.objects.select_related("workbench").get(pk=shape.pk)

    current_entry = _current_rule_entry(shape, rule_key)

    if kind == RuleChangeKind.ADDED:
        if current_entry is not None:
            raise RuleChangeError(
                f"Rule {rule_key} already exists on shape {shape.id} — edit it instead."
            )
        previous: dict[str, Any] = {}
        proposed = {
            f: (fields or {}).get(f)
            for f in CANVAS_RULE_ADD_FIELDS
            if (fields or {}).get(f) not in (None, "", [])
        }
        if not proposed.get("condition") and not proposed.get("action"):
            raise NoEffectiveChange("A new rule needs at least a condition or an action.")
    elif kind == RuleChangeKind.REMOVED:
        if current_entry is None:
            raise RuleChangeError(f"Rule {rule_key} not found on shape {shape.id}.")
        previous = {f: current_entry.get(f) for f in CANVAS_RULE_FIELDS}
        proposed = {}
    else:
        if current_entry is None:
            raise RuleChangeError(f"Rule {rule_key} not found on shape {shape.id}.")
        previous = {f: current_entry.get(f) for f in CANVAS_RULE_FIELDS}
        proposed = _sanitize_canvas_fields(fields or {}, previous)
        if not proposed:
            raise NoEffectiveChange(
                "No applicable field changes — edits must differ from the current "
                f"rule and target one of: {', '.join(CANVAS_RULE_FIELDS)}."
            )

    sop_id = (current_entry or {}).get("sop_id") or _rule_sop_id(rule_key)
    sop = _resolve_sop(sop_id) if sop_id else None

    change_set, _ = RuleChangeSet.objects.get_or_create(
        workflow=workflow,
        created_by=author,
        status=ChangeSetStatus.OPEN,
        source=ChangeSetSource.CANVAS,
        defaults={"base_version": workflow.version, "sop": sop},
    )
    if change_set.base_version != workflow.version:
        _rebase(change_set, workflow)
    elif sop is not None and change_set.sop_id is None:
        change_set.sop = sop
        change_set.save(update_fields=["sop", "updated_at"])

    display_id = ((current_entry or {}).get("subrule_id") or rule_key or "")[:64]
    proposal, _ = RuleChangeProposal.objects.update_or_create(
        changeset=change_set,
        shape_id=shape.id,
        rule_key=rule_key,
        defaults={
            "change_kind": kind,
            "subrule_id": ((current_entry or {}).get("subrule_id") or "")[:64],
            "display_rule_id": display_id,
            "workbench_id": shape.workbench_id,
            "node_key": (shape.workbench.node_key or "") if shape.workbench_id else "",
            "is_custom": bool(is_custom),
            "previous_fields": _jsonable(previous),
            "proposed_fields": _jsonable(proposed),
        },
    )
    change_set.save(update_fields=["updated_at"])
    return proposal


def _rebase(change_set: RuleChangeSet, workflow: Workflow) -> None:
    """Re-point a stale open canvas batch at the workflow's current rule state.

    Mirrors sop_ingestion.services.rule_changes._rebase: existing proposals
    keep the author's intent but have their ``previous_fields``
    re-snapshotted; a proposal already satisfied by the newer state, or whose
    target shape/rule no longer exists, is dropped.
    """
    for proposal in list(change_set.proposals.all()):
        shape = Shape.objects.filter(pk=proposal.shape_id).select_related("workbench").first()
        if shape is None:
            proposal.delete()
            continue
        current_entry = _current_rule_entry(shape, proposal.rule_key)

        if proposal.change_kind == RuleChangeKind.ADDED:
            if current_entry is not None:
                proposal.delete()  # someone else already added this rule_key
                continue
            still_differs = proposal.proposed_fields or {}
        else:
            if current_entry is None:
                proposal.delete()
                continue
            current = {f: current_entry.get(f) for f in CANVAS_RULE_FIELDS}
            if proposal.change_kind == RuleChangeKind.REMOVED:
                still_differs = proposal.proposed_fields or {}
            else:
                still_differs = _sanitize_canvas_fields(proposal.proposed_fields or {}, current)
                if not still_differs:
                    proposal.delete()
                    continue
            proposal.previous_fields = _jsonable(current)

        proposal.proposed_fields = _jsonable(still_differs)
        proposal.save(update_fields=["previous_fields", "proposed_fields", "updated_at"])

    change_set.base_version = workflow.version
    change_set.save(update_fields=["base_version", "updated_at"])


# ── Approve ──────────────────────────────────────────────────────────────────


def _refile_blocked_proposal(proposal: RuleChangeProposal, workflow: Workflow) -> None:
    """Move a proposal that failed ``require_approved_sop`` onto a fresh open
    canvas batch instead of losing it.

    The batch it came from is about to be marked APPROVED (for whatever else
    in it did apply) — leaving this proposal attached would bury it inside a
    closed change set with no further review path. Re-filing it under the
    same ``(workflow, created_by, canvas)`` key the author's next edit would
    use keeps it visible and re-approvable once the underlying SOP is
    rebound (e.g. via ``version-adopt``), without the author re-entering the
    edit from scratch.
    """
    target_set, _ = RuleChangeSet.objects.get_or_create(
        workflow=workflow,
        created_by=proposal.changeset.created_by,
        status=ChangeSetStatus.OPEN,
        source=ChangeSetSource.CANVAS,
        defaults={"base_version": workflow.version},
    )
    if target_set.pk != proposal.changeset_id:
        proposal.changeset = target_set
        proposal.save(update_fields=["changeset", "updated_at"])


def approve_canvas_change_set(
    change_set: RuleChangeSet,
    *,
    proposals: list[RuleChangeProposal],
    reviewer: str,
) -> dict[str, Any]:
    """Apply every proposal in the batch, then snapshot exactly one new
    WorkflowVersion. Called from ``rule_changes.approve_change_set`` only
    after it has already locked the change set and validated it is OPEN,
    not stale, and that ``proposals`` is exactly what the reviewer saw —
    this function does not re-check any of that.
    """
    workflow = change_set.workflow
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    blocked_proposals: list[RuleChangeProposal] = []

    for p in proposals:
        shape = Shape.objects.filter(pk=p.shape_id).first()
        if shape is None:
            skipped.append({"proposal_id": p.id, "rule_key": p.rule_key,
                            "reason": "shape_not_found"})
            continue

        props = dict(shape.properties or {})
        raw_rules = [dict(r) for r in (props.get("sop_rules") or []) if isinstance(r, dict)]
        idx = next((i for i, r in enumerate(raw_rules) if r.get("key") == p.rule_key), None)

        if p.change_kind == RuleChangeKind.REMOVED:
            if idx is None:
                skipped.append({"proposal_id": p.id, "rule_key": p.rule_key,
                                "reason": "rule_not_found"})
                continue
            raw_rules.pop(idx)
        elif p.change_kind == RuleChangeKind.ADDED:
            if idx is not None:
                skipped.append({"proposal_id": p.id, "rule_key": p.rule_key,
                                "reason": "rule_already_exists"})
                continue
            entry = {"key": p.rule_key, "is_custom": p.is_custom}
            entry.update(p.proposed_fields or {})
            raw_rules.append(entry)
        else:
            if idx is None:
                skipped.append({"proposal_id": p.id, "rule_key": p.rule_key,
                                "reason": "rule_not_found"})
                continue
            raw_rules[idx] = {**raw_rules[idx], **(p.proposed_fields or {})}

        props["sop_rules"] = raw_rules

        # Isolated in its own savepoint: require_approved_sop (fired inside
        # extract_bindings_from_properties, scoped to only the rule(s) that
        # actually changed this call — see its content_unchanged guard) can
        # legitimately reject THIS proposal without rolling back every other
        # proposal already applied in this loop. Without this savepoint, one
        # stale-SOP proposal sitting in the same author's open batch would
        # abort the whole approval — including an unrelated custom rule add/
        # edit bundled in with it.
        try:
            with transaction.atomic():
                shape.properties = props
                shape.save(update_fields=["properties"])
                extract_bindings_from_properties(shape)
        except ValidationError as exc:
            blocked.append({"proposal_id": p.id, "rule_key": p.rule_key,
                            "reason": "stale_sop_binding", "detail": str(exc.detail)})
            blocked_proposals.append(p)
            continue

        applied.append({"proposal_id": p.id, "rule_key": p.rule_key,
                        "change_kind": p.change_kind})

    if not applied:
        raise NoEffectiveChange(
            "No proposals could be applied — every target rule/shape was "
            "missing or blocked by a stale SOP binding."
        )

    from .workflow_versioning import snapshot_workflow_version

    # Applied above, THEN snapshotted once: snapshot_workflow_version derives
    # WorkflowVersionRule from a fresh read of the workflow's live rule set,
    # so calling it once, after every proposal in the batch has landed,
    # guarantees exactly one WorkflowVersion containing the complete
    # post-change state (not one per proposal, not a partial mid-batch view).
    snapshot = snapshot_workflow_version(workflow, reason="rule_edit", force=True)

    change_set.status = ChangeSetStatus.APPROVED
    change_set.reviewed_by = (reviewer or "").strip()[:128]
    change_set.reviewed_at = timezone.now()
    change_set.resulting_version = snapshot.version_number if snapshot else workflow.version
    change_set.save(update_fields=[
        "status", "reviewed_by", "reviewed_at", "resulting_version", "updated_at",
    ])

    # Re-file blocked proposals only AFTER this batch is saved as APPROVED —
    # get_or_create's (workflow, created_by, OPEN, canvas) lookup would
    # otherwise just find THIS batch again (still OPEN at the time the loop
    # above ran) and leave the proposal exactly where it started.
    for p in blocked_proposals:
        _refile_blocked_proposal(p, workflow)

    return {
        "changeset_id": change_set.id,
        "source": ChangeSetSource.CANVAS,
        "workflow_id": workflow.id,
        "resulting_version": change_set.resulting_version,
        "applied": applied,
        "skipped": skipped,
        "blocked": blocked,
    }


# ── Read ─────────────────────────────────────────────────────────────────────


def _proposal_payload(proposal: RuleChangeProposal) -> dict[str, Any]:
    previous = proposal.previous_fields or {}
    proposed = proposal.proposed_fields or {}
    kind = proposal.change_kind

    if kind == RuleChangeKind.REMOVED:
        resulting: dict[str, Any] = {}
        changed = sorted(f for f in CANVAS_RULE_FIELDS if previous.get(f))
    elif kind == RuleChangeKind.ADDED:
        previous = {}
        resulting = proposed
        changed = sorted(f for f in CANVAS_RULE_FIELDS if proposed.get(f))
    else:
        resulting = {**previous, **proposed}
        changed = sorted(proposed.keys())

    def _render(source: dict[str, Any]) -> dict[str, Any]:
        return {f: source.get(f) for f in CANVAS_RULE_FIELDS}

    title_src = (previous or resulting).get("condition") or proposal.display_rule_id or proposal.rule_key
    title = str(title_src or "")
    if len(title) > 70:
        title = title[:70] + "…"

    return {
        "id": proposal.id,
        "shape_id": str(proposal.shape_id) if proposal.shape_id else None,
        "workbench_id": str(proposal.workbench_id) if proposal.workbench_id else None,
        "node_key": proposal.node_key,
        "rule_key": proposal.rule_key,
        "change_kind": proposal.change_kind,
        "is_custom": proposal.is_custom,
        "display_rule_id": proposal.display_rule_id,
        "subrule_id": proposal.subrule_id,
        "title": title,
        "fields_changed": changed,
        "previous": _render(previous),
        "current": _render(resulting),
    }


def canvas_change_set_payload(
    change_set: RuleChangeSet, *, include_proposals: bool = False,
) -> dict[str, Any]:
    """Serialise a canvas-sourced change set — the same envelope shape as
    sop_ingestion.services.rule_changes.change_set_payload, adapted for a
    Shape/rule_key target instead of an AuditDecision, and a workflow rather
    than a document version pair."""
    from sop_ingestion.services.rule_changes import _kind_summary

    proposals = list(change_set.proposals.all())
    stale = change_set.is_stale
    sop = change_set.sop

    payload: dict[str, Any] = {
        "id": change_set.id,
        "status": (ChangeSetStatus.STALE if stale and change_set.status == ChangeSetStatus.OPEN
                  else change_set.status),
        "stale": stale,
        "sop": (
            {
                "id": sop.id,
                "title": sop.title or "",
                "version": sop.version,
                "version_number": sop.version_number,
            } if sop is not None else None
        ),
        "workflow": {
            "id": str(change_set.workflow_id) if change_set.workflow_id else None,
            "name": change_set.workflow.name if change_set.workflow_id else "",
        },
        "workflow_names": [change_set.workflow.name] if change_set.workflow_id else [],
        "from_version": change_set.base_version,
        "to_version": change_set.base_version + 1,
        "source": change_set.source,
        "summary": _kind_summary(proposals),
        "proposal_count": len(proposals),
        "created_by": change_set.created_by,
        "created_at": change_set.created_at,
        "updated_at": change_set.updated_at,
        "reviewed_by": change_set.reviewed_by,
        "reviewed_at": change_set.reviewed_at,
        "review_note": change_set.review_note,
        "resulting_version": change_set.resulting_version,
    }
    if include_proposals:
        payload["proposals"] = [_proposal_payload(p) for p in proposals]
    return payload