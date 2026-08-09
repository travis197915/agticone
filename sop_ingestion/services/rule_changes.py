"""Auditor-authored rule edits: propose → review → apply.

An auditor edits a SOP rule in the claims frontend. The edit does NOT touch the
canonical ``AuditDecision`` — it lands as a :class:`RuleChangeProposal`, batched
into a :class:`RuleChangeSet`, and waits for review in the audit dashboard.

Approving a change set hands its proposals to :func:`rule_reconcile.apply`,
which bumps ``AuditDecision.revision`` per rule and ``AuditSop.version`` once
per batch — exactly the ``v1 → v2`` transition the review modal renders.

Two version fields on ``AuditSop`` are easy to confuse:

``version_number``
    Which ingested *document* snapshot this is. Moves on re-crawl/re-upload.
``version``
    The *rule-content* counter. Moves when rules are edited. **This is the one
    this module pins and compares against** — it is what ``apply()`` bumps.

Batching is *implicit*: an edit finds-or-creates the author's open change set
for that SOP, so a session's worth of edits reviews together. Scoping to
``(sop, author)`` keeps two auditors working the same SOP from sharing a fate —
one reviewer's Reject cannot kill the other's work.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone

from ..models import (
    AuditDecision,
    ChangeSetSource,
    ChangeSetStatus,
    RuleChangeKind,
    RuleChangeProposal,
    RuleChangeSet,
)
# Imported from the reconcile engine on purpose. Validating a proposal with the
# *same* sanitiser ``apply()`` will use guarantees we never accept an edit at
# propose time that gets silently dropped at apply time.
from ..rule_reconcile import (  # noqa: F401  (RECONCILABLE_FIELDS re-exported)
    RECONCILABLE_FIELDS,
    _decision_fields,
    _rule_key,
    _sanitize_proposed,
)
from ..rule_reconcile import apply as _apply_reconciled

logger = logging.getLogger(__name__)

__all__ = [
    "RuleChangeError",
    "ChangeSetClosed",
    "ChangeSetStale",
    "ChangeSetMoved",
    "NoEffectiveChange",
    "RECONCILABLE_FIELDS",
    "scrub_mojibake",
    "propose_rule_change",
    "build_change_set_from_ingestion",
    "list_change_sets",
    "change_set_payload",
    "pending_proposals_for_sop",
    "approve_change_set",
    "reject_change_set",
]


# ── Errors ───────────────────────────────────────────────────────────────────
# All subclass ValueError so views can follow the existing sop_ingestion
# convention of mapping ValueError → 409, while ``code`` gives the frontend a
# stable discriminator to branch on.


class RuleChangeError(ValueError):
    code = "rule_change_error"


class ChangeSetClosed(RuleChangeError):
    code = "changeset_closed"


class ChangeSetStale(RuleChangeError):
    code = "changeset_stale"


class ChangeSetMoved(RuleChangeError):
    code = "changeset_moved"


class NoEffectiveChange(RuleChangeError):
    code = "no_effective_change"


# ── Mojibake ────────────────────────────────────────────────────────────────
# Some ingested SOP text carries UTF-8 bytes that were rendered through CP437
# at ingestion time, so a bullet arrives as "ΓÇó". Left alone it renders
# straight into the review modal. Scrubbed on read; fixing it at the source is
# a separate cleanup.

_MOJIBAKE = {
    "ΓÇö": "—",  # em dash
    "ΓÇô": "–",  # en dash
    "ΓÇó": "•",  # bullet
    "ΓÇÖ": "’",  # right single quote
    "ΓÇÿ": "‘",  # left single quote
    "ΓÇ£": "“",  # left double quote
    "ΓÇ¥": "”",  # right double quote
    "ΓÇª": "…",  # ellipsis
    "Γû¬": "▪",  # black small square
    "Γùï": "○",  # white circle
    "Γùª": "◦",  # white bullet
    "Γé¼": "€",  # euro
}
# Longest first so no key is a prefix of another mid-replacement.
_MOJIBAKE_RE = re.compile(
    "|".join(re.escape(k) for k in sorted(_MOJIBAKE, key=len, reverse=True))
)


def scrub_mojibake(text: str) -> str:
    """Repair CP437-rendered UTF-8 punctuation in ingested SOP text."""
    if not text:
        return ""
    return _MOJIBAKE_RE.sub(lambda m: _MOJIBAKE[m.group(0)], text)


# ── Derivations ─────────────────────────────────────────────────────────────

_TITLE_MAX = 70


def display_rule_id(decision: AuditDecision) -> str:
    """Human-facing rule label for the review modal's left rail.

    ``subrule_id`` coverage ranges 26%–100% across SOPs, so a positional
    fallback is the primary path on some of them. Stored on the proposal rather
    than re-derived, so the modal stays stable if the SOP is later re-ingested.
    """
    sid = (decision.subrule_id or "").strip()
    if sid:
        return sid[:64]
    return f"Step {decision.step.step_number} · Row {decision.row_index}"[:64]


def rule_title(fields: dict[str, Any]) -> str:
    """Short title line, derived from the rule text.

    ``action_summary`` and ``output_text`` are populated on roughly 1 row in 58,
    so neither is viable. The first line of ``condition_if`` reads acceptably
    ("New day claim Submission") and is what we use.
    """
    raw = str(fields.get("condition_if") or "").strip()
    if not raw:
        raw = str(fields.get("action_text") or "").strip()
    if not raw:
        return ""
    first = scrub_mojibake(raw).splitlines()[0].strip(" \t•-–—")
    return f"{first[:_TITLE_MAX]}…" if len(first) > _TITLE_MAX else first


def _safe_node_rule_binding():
    try:
        from agent_tools.models import NodeRuleBinding
    except Exception:  # pragma: no cover - agent_tools optional at import time
        return None
    return NodeRuleBinding


def workflow_names_by_sop(sop_ids: Iterable[int]) -> dict[int, list[str]]:
    """``{sop_id: [workflow name, ...]}`` in one query.

    Feeds the modal header ("OBH CLAIM AUDIT FLOW · 3 rules changed"). The
    richer per-rule cross-workflow impact belongs to the impact resolver.
    """
    sop_ids = [s for s in sop_ids if s]
    NodeRuleBinding = _safe_node_rule_binding()
    if NodeRuleBinding is None or not sop_ids:
        return {}
    rows = (
        NodeRuleBinding.objects
        .filter(sop_id__in=sop_ids)
        .values_list("sop_id", "shape__workbench__work_area__workflow__name")
        .distinct()
    )
    out: dict[int, list[str]] = defaultdict(list)
    for sop_id, name in rows:
        if name and name not in out[sop_id]:
            out[sop_id].append(name)
    return dict(out)


# ── Propose ─────────────────────────────────────────────────────────────────


@transaction.atomic
def propose_rule_change(
    *,
    decision: AuditDecision,
    fields: dict[str, Any],
    author: str,
) -> RuleChangeProposal:
    """Record a proposed edit to one rule. Does not touch the canonical rule.

    Finds-or-creates the author's open change set for this rule's SOP, then
    upserts the proposal — re-editing the same rule before review updates the
    existing row rather than listing that rule twice in the modal.

    Raises :class:`NoEffectiveChange` when nothing survives sanitisation, which
    also covers "the user retyped the same text".
    """
    author = (author or "").strip()[:128]
    # Re-read rather than trusting the caller's instance. A decision fetched
    # before another author's batch landed carries a stale ``sop.version`` and
    # stale rule text, which would skip the re-base below and snapshot a
    # ``previous_fields`` that no longer matches the database.
    decision = (
        AuditDecision.objects
        .select_related("step", "step__sop")
        .get(pk=decision.pk)
    )
    step = decision.step
    sop = step.sop

    previous = _decision_fields(decision)
    proposed = _sanitize_proposed(fields or {}, previous)
    if not proposed:
        raise NoEffectiveChange(
            "No applicable field changes — edits must differ from the current "
            f"rule and target one of: {', '.join(RECONCILABLE_FIELDS)}."
        )

    change_set, _ = RuleChangeSet.objects.get_or_create(
        sop=sop,
        created_by=author,
        status=ChangeSetStatus.OPEN,
        defaults={"base_version": sop.version},
    )
    # Someone else's batch may have landed since this one opened, which would
    # leave it permanently un-approvable. Re-base it now, while the author is
    # here, rather than stranding their work behind a conflict they cannot
    # clear from the UI.
    if change_set.base_version != sop.version:
        _rebase(change_set, sop)

    from .rule_impact import dependent_steps_for_decisions

    proposal, _ = RuleChangeProposal.objects.update_or_create(
        changeset=change_set,
        decision=decision,
        defaults={
            "subrule_id": (decision.subrule_id or "")[:64],
            "display_rule_id": display_rule_id(decision),
            "step_number": step.step_number,
            "row_index": decision.row_index,
            "base_revision": decision.revision or 1,
            "previous_fields": _jsonable(previous),
            "proposed_fields": _jsonable(proposed),
            # Snapshot the routing impact now; refreshed again on read, since
            # the rule's own action text may change before review.
            "dependent_steps": dependent_steps_for_decisions([decision]).get(
                decision.id, []
            ),
        },
    )
    # Touch the parent so the reviewer's list sorts by most-recent activity.
    change_set.save(update_fields=["updated_at"])
    return proposal


@transaction.atomic
def build_change_set_from_ingestion(
    *,
    workflow,
    from_sop,
    to_sop,
    job=None,
    author: str = "",
) -> RuleChangeSet | None:
    """Turn a re-ingested SOP into a reviewable batch for one workflow.

    Returns ``None`` when nothing changed — an unchanged re-ingest must not
    raise a review, and neither must a SOP the workflow has never had (the
    caller simply does not call this when there is no ``from_sop``).

    Re-uploading the same SOP before review **replaces** the open batch's
    proposals rather than queueing a second review. That is the ingestion
    equivalent of ``_rebase``: the newer upload is the author's current intent,
    and leaving the older diff around would let a reviewer approve a rollout to
    a version that has already been superseded.
    """
    from .sop_rule_diff import diff_sop_versions

    deltas = diff_sop_versions(from_sop, to_sop)
    if not deltas:
        return None

    author = (author or "").strip()[:128]
    change_set, _ = RuleChangeSet.objects.get_or_create(
        sop=from_sop,
        workflow=workflow,
        created_by=author,
        status=ChangeSetStatus.OPEN,
        defaults={
            "base_version": from_sop.version,
            "source": ChangeSetSource.INGESTION,
            "to_sop": to_sop,
            "ingestion_job": job,
        },
    )
    # An existing open batch is superseded by this upload, not merged with it —
    # the two diffs are against different incoming versions and cannot be
    # meaningfully interleaved.
    change_set.proposals.all().delete()
    change_set.base_version = from_sop.version
    change_set.source = ChangeSetSource.INGESTION
    change_set.to_sop = to_sop
    change_set.ingestion_job = job
    change_set.save(update_fields=[
        "base_version", "source", "to_sop", "ingestion_job", "updated_at",
    ])

    RuleChangeProposal.objects.bulk_create([
        _proposal_from_delta(change_set, delta) for delta in deltas
    ])
    return change_set


def _proposal_from_delta(change_set: RuleChangeSet, delta) -> RuleChangeProposal:
    """Build (unsaved) a proposal row from one diff entry.

    Identity comes from whichever side exists — the old rule for a
    modification or removal, the new one for an addition — so the reviewer sees
    the label they would recognise.
    """
    anchor = delta.anchor
    return RuleChangeProposal(
        changeset=change_set,
        decision=delta.from_decision,
        to_decision=delta.to_decision,
        change_kind=delta.kind,
        subrule_id=(anchor.subrule_id or "")[:64],
        display_rule_id=display_rule_id(anchor),
        step_number=anchor.step.step_number,
        row_index=anchor.row_index,
        base_revision=(delta.from_decision.revision if delta.from_decision else 1) or 1,
        previous_fields=delta.previous_fields,
        proposed_fields=delta.proposed_fields,
        # Routing impact is resolved from the rule that will be in force —
        # the incoming one where there is one.
        dependent_steps=_dependent_steps_for(delta.to_decision or delta.from_decision),
    )


def _dependent_steps_for(decision) -> list:
    if decision is None:
        return []
    from .rule_impact import dependent_steps_for_decisions
    return dependent_steps_for_decisions([decision]).get(decision.id, [])


def _rebase(change_set: RuleChangeSet, sop) -> None:
    """Re-point a stale open batch at the SOP's current rule text.

    Existing proposals keep the author's intent (``proposed_fields``) but have
    their ``previous_fields`` re-snapshotted, so the review modal diffs against
    text that actually exists. A proposal whose intent is already satisfied by
    the newer text is dropped — otherwise the modal would render two identical
    panes and the reviewer would wonder what they were approving.
    """
    for proposal in change_set.proposals.select_related("decision__step"):
        current = _decision_fields(proposal.decision)
        still_differs = _sanitize_proposed(proposal.proposed_fields or {}, current)
        if not still_differs:
            proposal.delete()
            continue
        proposal.previous_fields = _jsonable(current)
        proposal.proposed_fields = _jsonable(still_differs)
        proposal.base_revision = proposal.decision.revision or 1
        proposal.save(update_fields=[
            "previous_fields", "proposed_fields", "base_revision", "updated_at",
        ])

    change_set.base_version = sop.version
    change_set.save(update_fields=["base_version", "updated_at"])


def _jsonable(fields: dict[str, Any]) -> dict[str, Any]:
    """Coerce decision field values into JSON-safe primitives."""
    out: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if isinstance(value, bool) or value is None:
            out[key] = value
        elif isinstance(value, (int, float, str)):
            out[key] = value
        else:
            out[key] = str(value)
    return out


# ── Read ────────────────────────────────────────────────────────────────────


def list_change_sets(
    *,
    status: str | None = ChangeSetStatus.OPEN,
    sop_id: int | None = None,
    created_by: str | None = None,
    workflow_id: str | None = None,
    source: str | None = None,
):
    qs = (
        RuleChangeSet.objects
        .select_related("sop", "to_sop", "workflow")
        .prefetch_related("proposals")
    )
    if status:
        qs = qs.filter(status=status)
    if sop_id:
        qs = qs.filter(sop_id=sop_id)
    if created_by:
        qs = qs.filter(created_by=created_by)
    if workflow_id:
        qs = qs.filter(workflow_id=workflow_id)
    if source:
        qs = qs.filter(source=source)
    return qs


def change_set_payload(
    change_set: RuleChangeSet,
    *,
    include_proposals: bool = False,
    workflow_names: list[str] | None = None,
) -> dict[str, Any]:
    """Serialise a change set for the bell (summary) or the modal (detail)."""
    sop = change_set.sop
    proposals = list(
        change_set.proposals.select_related(
            "decision", "decision__step", "to_decision", "to_decision__step",
        )
    ) if include_proposals else list(change_set.proposals.all())
    stale = change_set.is_stale
    ingestion = change_set.source == ChangeSetSource.INGESTION

    payload: dict[str, Any] = {
        "id": change_set.id,
        "status": ChangeSetStatus.STALE if stale and change_set.status == ChangeSetStatus.OPEN
                  else change_set.status,
        "stale": stale,
        "sop": {
            "id": sop.id,
            "title": scrub_mojibake(sop.title or ""),
            "version": sop.version,
            "version_number": sop.version_number,
        },
        # For an ingestion batch the blast radius is exactly one workflow by
        # construction, so listing every workflow that uses the SOP would
        # overstate it — the whole point of the scoping rule is that the others
        # are untouched.
        "workflow_names": (
            [change_set.workflow.name] if ingestion and change_set.workflow_id
            else (workflow_names if workflow_names is not None else [])
        ),
        # The modal header renders "v{from} → v{to}". Which pair of numbers
        # that means depends on where the batch came from: an auditor's edit
        # moves the rule-content counter, a re-upload moves the *document*
        # snapshot. Showing base_version for an ingestion batch would claim a
        # rule edit that never happened.
        "from_version": (sop.version_number if ingestion else change_set.base_version),
        "to_version": (
            change_set.to_sop.version_number if ingestion and change_set.to_sop_id
            else change_set.base_version + 1
        ),
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
    if ingestion:
        payload["workflow"] = {
            "id": str(change_set.workflow_id) if change_set.workflow_id else None,
            "name": change_set.workflow.name if change_set.workflow_id else "",
        }
        payload["to_sop"] = {
            "id": change_set.to_sop_id,
            "version": change_set.to_sop.version if change_set.to_sop_id else None,
            "version_number": (change_set.to_sop.version_number
                               if change_set.to_sop_id else None),
        }
    if include_proposals:
        from .rule_impact import resolve_for_proposals

        # Two queries for the whole batch, not two per proposal.
        impact = resolve_for_proposals(proposals)
        payload["proposals"] = [
            _proposal_payload(p, impact.get(p.id)) for p in proposals
        ]
    return payload


def _step_label_for(proposal: RuleChangeProposal) -> str:
    """The question of the step this rule belongs to, or "" if unavailable.

    Read from whichever side of the proposal exists — the old rule for a
    modification or removal, the incoming one for an addition. ``AuditStep.question``
    is empty on step 0 of most SOPs, in which case there is nothing honest to
    show and the UI falls back to the bare step number.
    """
    decision = proposal.decision or proposal.to_decision
    if decision is None:
        return ""
    try:
        return scrub_mojibake((decision.step.question or "").strip())
    except Exception:  # pragma: no cover - step row missing is not worth failing on
        return ""


def _kind_summary(proposals: list[RuleChangeProposal]) -> dict[str, int]:
    """Counts the review header shows: "3 modified · 9 new · 8 removed"."""
    summary = {"modified": 0, "added": 0, "removed": 0}
    for p in proposals:
        summary[p.change_kind] = summary.get(p.change_kind, 0) + 1
    return summary


def _proposal_payload(
    proposal: RuleChangeProposal,
    impact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = proposal.previous_fields or {}
    proposed = proposal.proposed_fields or {}
    kind = proposal.change_kind

    # Send the COMPLETE before/after state, not just the touched fields. The
    # review modal renders Condition and Action side by side regardless of
    # which one was edited — an unchanged field shows identical text in both
    # panes, which is how the reviewer sees the rule in context.
    # ``fields_changed`` stays the discriminator for highlighting.
    #
    # That merge is right for a modification and wrong for the other two kinds.
    # A removal has no proposed fields, so ``{**previous, **proposed}`` returns
    # the old rule unchanged and the modal renders the same text twice, both
    # panes badged "unchanged" — which reads as "nothing happened" for a rule
    # that is being deleted. An addition has no previous side at all. Each kind
    # gets the shape that is true of it.
    if kind == RuleChangeKind.REMOVED:
        resulting: dict[str, Any] = {}
        changed = sorted(f for f in RECONCILABLE_FIELDS if previous.get(f))
    elif kind == RuleChangeKind.ADDED:
        previous = {}
        resulting = proposed
        changed = sorted(f for f in RECONCILABLE_FIELDS if proposed.get(f))
    else:
        resulting = {**previous, **proposed}
        changed = sorted(proposed.keys())

    def _render(source: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field in RECONCILABLE_FIELDS:
            value = source.get(field)
            out[field] = scrub_mojibake(value) if isinstance(value, str) else value
        return out
    change_set = proposal.changeset
    # An added rule only exists on the new SOP, so its key is on that side.
    # Everything else is anchored to the version the workflow is on today.
    key_sop_id = (
        change_set.to_sop_id
        if proposal.change_kind == RuleChangeKind.ADDED and change_set.to_sop_id
        else change_set.sop_id
    )
    to_decision = proposal.to_decision
    return {
        "id": proposal.id,
        "decision_id": proposal.decision_id,
        "to_decision_id": proposal.to_decision_id,
        "change_kind": proposal.change_kind,
        "display_rule_id": proposal.display_rule_id,
        "subrule_id": proposal.subrule_id,
        # An addition has no previous text to name it by.
        "title": rule_title(previous or resulting),
        "step_number": proposal.step_number,
        # The step this rule *lives in*. Without it the only step number on the
        # screen is the routing target from ``dependent_steps`` ("Routes to
        # Step 6"), which reads as the rule's location and is a different step
        # entirely.
        "step_label": _step_label_for(proposal),
        "row_index": proposal.row_index,
        "rule_key": _rule_key(key_sop_id, proposal.step_number, proposal.row_index),
        # Where this rule lands after approval — the key Phase 5 has to re-run
        # against once the workflow has moved. Absent for a removal.
        "to_rule_key": (
            _rule_key(change_set.to_sop_id, to_decision.step.step_number,
                      to_decision.row_index)
            if to_decision is not None and change_set.to_sop_id else None
        ),
        "base_revision": proposal.base_revision,
        "fields_changed": changed,
        "previous": _render(previous),
        "current": _render(resulting),
        # Refreshed at read time — the rule's routing or the builder graph may
        # have moved since the edit was proposed, and a stale blast radius is
        # worse than none. Falls back to the propose-time snapshot.
        "dependent_steps": (impact or {}).get(
            "dependent_steps", proposal.dependent_steps or []
        ),
        "affected_workflows": (impact or {}).get("affected_workflows", []),
    }


def pending_proposals_for_sop(sop_id: int) -> dict[str, Any]:
    """``{decision_id: {changeset_id, proposed_fields}}`` for open proposals.

    Lets the editing UI badge rules that already carry an unreviewed edit, and
    render the *proposed* text rather than the canonical text — otherwise the
    author revisits, sees the old wording, and assumes the save failed.
    """
    rows = (
        RuleChangeProposal.objects
        .filter(changeset__sop_id=sop_id, changeset__status=ChangeSetStatus.OPEN)
        .values("decision_id", "changeset_id", "proposed_fields",
                "display_rule_id", "changeset__created_by")
    )
    return {
        str(r["decision_id"]): {
            "changeset_id": r["changeset_id"],
            "proposed_fields": r["proposed_fields"],
            "display_rule_id": r["display_rule_id"],
            "created_by": r["changeset__created_by"],
        }
        for r in rows
    }


# ── Review ──────────────────────────────────────────────────────────────────


@transaction.atomic
def approve_change_set(
    change_set: RuleChangeSet,
    *,
    proposal_ids: Iterable[int],
    reviewer: str,
) -> dict[str, Any]:
    """Apply every proposal in the batch, then close it.

    ``proposal_ids`` must name exactly the proposals the reviewer saw. Implicit
    batching means the author can add a rule while the modal is open; without
    this check that rule would ship unreviewed. A mismatch is a 409 and the
    reviewer refreshes.
    """
    change_set = (
        RuleChangeSet.objects
        # ``of=("self",)`` because ``to_sop``/``workflow`` are nullable and
        # Postgres refuses FOR UPDATE on the nullable side of an outer join.
        # The change set row is the only one that needs locking regardless.
        .select_for_update(of=("self",))
        .select_related("sop", "to_sop", "workflow")
        .get(pk=change_set.pk)
    )
    if change_set.status != ChangeSetStatus.OPEN:
        raise ChangeSetClosed(
            f"Change set {change_set.id} is {change_set.status}, not open."
        )
    if change_set.is_stale:
        # Deliberately NOT persisted as STALE. This function is atomic, so the
        # write would roll back with the raise anyway — and leaving the row
        # ``open`` keeps it visible in the reviewer's inbox, where
        # ``change_set_payload`` reports ``stale: true`` so the UI can flag it.
        # Marking it STALE would hide a batch that still needs attention.
        # The author's next edit re-bases it (see ``_rebase``).
        raise ChangeSetStale(
            f"SOP rule version moved to v{change_set.sop.version} since this "
            f"batch opened at v{change_set.base_version}. Re-diff before applying."
        )

    proposals = list(change_set.proposals.select_related("decision__step"))
    seen = {int(p) for p in (proposal_ids or [])}
    current = {p.id for p in proposals}
    if seen != current:
        raise ChangeSetMoved(
            "This change set moved since it was opened — refresh and review again."
        )
    if not proposals:
        raise NoEffectiveChange("Change set has no proposals to apply.")

    if change_set.source == ChangeSetSource.INGESTION:
        return _approve_ingestion(change_set, reviewer)

    sop = change_set.sop
    accepted = [
        {
            # apply() routes AUGMENT and CONTRADICT down the same update path;
            # CONTRADICT is the closer fit for a direct edit, which replaces
            # rather than merges into the prior text.
            "verdict": "CONTRADICT",
            "decision_id": p.decision_id,
            "subrule_id": p.subrule_id,
            "proposed": p.proposed_fields or {},
            "rule_key": _rule_key(sop.id, p.step_number, p.row_index),
            "reason": f"Auditor edit — change set #{change_set.id} by {change_set.created_by}",
        }
        for p in proposals
    ]

    result = _apply_reconciled(
        sop,
        accepted,
        user=(reviewer or "")[:128] or "unknown",
        yaml_source="auditor_edit",
        reconcile_id=f"changeset:{change_set.id}",
    )

    change_set.status = ChangeSetStatus.APPROVED
    change_set.reviewed_by = (reviewer or "").strip()[:128]
    change_set.reviewed_at = timezone.now()
    change_set.resulting_version = result.get("version")
    change_set.save(update_fields=[
        "status", "reviewed_by", "reviewed_at", "resulting_version", "updated_at",
    ])

    result["changeset_id"] = change_set.id
    result["resulting_version"] = change_set.resulting_version
    return result


def _activate_reviewed_version(sop, previous, reviewer: str) -> str:
    """Promote the reviewed version once its rules are live on the canvas.

    Best-effort on purpose: the canvas has already been repointed by the time
    this runs, so failing the approval over a document-level flag would leave
    the workflow rolled forward with the batch still open — strictly worse than
    an activation that has to be retried. The mismatch is logged and returned
    so the caller can surface it.

    Two paths, because ``AuditSop.document`` is nullable and plenty of live SOPs
    have none (SOP 7, the Timely Filing doc, is one). ``activate_sop_version``
    *requires* a document and raises without one — which used to leave the
    canvas running v2 while v2 still read "Pending review" and v1 still claimed
    "Current" with nothing bound to it. Exactly the inconsistency the pending
    lifecycle was introduced to remove, reached from the other side. A SOP with
    no document is legitimate data, so it gets the same state transition
    directly rather than being left unactivatable.
    """
    from django.db import transaction

    from ..models import ActivationStatus, AuditSop
    from .versioning import activate_sop_version

    if sop is None:
        return "no_target_sop"
    if sop.activation_status == ActivationStatus.ACTIVE and sop.is_current:
        return "already_active"

    if sop.document_id:
        try:
            activate_sop_version(sop.id, reviewed_by=reviewer or "unknown")
            return "activated"
        except Exception as exc:
            logger.warning("could not activate sop=%s on approval: %s", sop.id, exc)
            return f"activation_failed: {exc}"

    try:
        with transaction.atomic():
            sop.is_current = True
            sop.activation_status = ActivationStatus.ACTIVE
            sop.save(update_fields=["is_current", "activation_status", "updated_at"])
            if previous is not None and previous.pk != sop.pk:
                AuditSop.objects.filter(pk=previous.pk).update(
                    is_current=False,
                    activation_status=ActivationStatus.SUPERSEDED,
                )
        logger.info(
            "activated sop=%s (no SopDocument; superseded sop=%s directly)",
            sop.id, getattr(previous, "id", None),
        )
        return "activated_without_document"
    except Exception as exc:
        logger.warning("could not activate document-less sop=%s: %s", sop.id, exc)
        return f"activation_failed: {exc}"


def _reject_reviewed_version(sop, reviewer: str, note: str) -> str:
    """Mark a parked version rejected. Best-effort, same reasoning as activate."""
    from ..models import ActivationStatus
    from .versioning import reject_sop_version

    if sop is None or sop.activation_status != ActivationStatus.PENDING_REVIEW:
        return "not_pending"
    try:
        reject_sop_version(sop.id, reviewed_by=reviewer or "unknown", reason=note or "")
        return "rejected"
    except Exception as exc:
        logger.warning("could not reject sop=%s: %s", sop.id, exc)
        return f"rejection_failed: {exc}"


def _approve_ingestion(change_set: RuleChangeSet, reviewer: str) -> dict[str, Any]:
    """Approve a re-ingestion batch by rolling one workflow onto the new SOP.

    Deliberately *not* the ``rule_reconcile.apply()`` path the manual flow
    uses. That path writes the proposed text onto the existing decision rows,
    which is right for an auditor's edit — there is no other version of the
    rule to point at. Here the new rows already exist, ingested from the new
    document, so applying would fork the old SOP into a hybrid that matches
    neither uploaded file and destroy the provenance the version exists for.

    ``AuditSop.is_current`` is left alone on purpose: it is a document-level
    flag, and a SOP update is scoped to the workflow it was uploaded on. Which
    version a workflow runs is answered by its bindings, not by that flag.
    """
    from .workflow_rollout import roll_workflow_forward

    if change_set.workflow_id is None or change_set.to_sop_id is None:
        raise RuleChangeError(
            f"Change set {change_set.id} is marked as an ingestion batch but is "
            "missing its workflow or target SOP."
        )

    report = roll_workflow_forward(
        workflow=change_set.workflow,
        from_sop=change_set.sop,
        to_sop=change_set.to_sop,
    )

    # Approval is what adopts the version. Ingestion only parked it at
    # ``pending_review`` — until this point the workflow was still executing
    # ``change_set.sop``, and the badge strip said so.
    _activate_reviewed_version(change_set.to_sop, change_set.sop, reviewer)

    change_set.status = ChangeSetStatus.APPROVED
    change_set.reviewed_by = (reviewer or "").strip()[:128]
    change_set.reviewed_at = timezone.now()
    change_set.resulting_version = change_set.to_sop.version
    change_set.save(update_fields=[
        "status", "reviewed_by", "reviewed_at", "resulting_version", "updated_at",
    ])

    return {
        "changeset_id": change_set.id,
        "source": ChangeSetSource.INGESTION,
        "workflow_id": change_set.workflow_id,
        "from_sop_id": change_set.sop_id,
        "to_sop_id": change_set.to_sop_id,
        "resulting_version": change_set.resulting_version,
        "rollout": report.as_dict(),
    }


@transaction.atomic
def reject_change_set(
    change_set: RuleChangeSet,
    *,
    reviewer: str,
    note: str = "",
) -> RuleChangeSet:
    """Close the batch without applying anything.

    Proposals are retained for audit but are dead — the author re-edits from
    scratch. They are not notified in-app; that is deliberate for v1.
    """
    change_set = (
        RuleChangeSet.objects
        .select_for_update(of=("self",))
        .select_related("to_sop")
        .get(pk=change_set.pk)
    )
    if change_set.status != ChangeSetStatus.OPEN:
        raise ChangeSetClosed(
            f"Change set {change_set.id} is {change_set.status}, not open."
        )
    if change_set.source == ChangeSetSource.INGESTION and change_set.to_sop_id:
        # The version was parked at pending_review by ingestion and nothing
        # else will ever clear it, so rejecting the batch has to reject the
        # version too — otherwise it sits pending forever, badged as neither
        # live nor dead.
        _reject_reviewed_version(change_set.to_sop, reviewer, note)
    change_set.status = ChangeSetStatus.REJECTED
    change_set.reviewed_by = (reviewer or "").strip()[:128]
    change_set.reviewed_at = timezone.now()
    change_set.review_note = (note or "").strip()
    change_set.save(update_fields=[
        "status", "reviewed_by", "reviewed_at", "review_note", "updated_at",
    ])
    return change_set
