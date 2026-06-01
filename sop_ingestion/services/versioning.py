"""SOP version registration and structural diff against persisted audit rows."""
from __future__ import annotations

import logging
from typing import Any

from django.db import transaction

from uhc_sop_ingestion.revision import (
    normalize_canonical_url,
    normalize_revision_date,
    revision_dates_equal,
)

from ..models import (
    ActivationStatus,
    AuditCode,
    AuditDecision,
    AuditPrecondition,
    AuditSop,
    AuditStep,
    SopDocument,
    SopVersionDiff,
)

log = logging.getLogger(__name__)

VERSION_NEW = "NEW"
VERSION_UNCHANGED = "UNCHANGED"
VERSION_REVISED = "REVISED"
VERSION_CONTENT_CHANGE = "CONTENT_CHANGE"


def classify_version_action(
    *,
    prior: AuditSop | None,
    revision_date: str,
    content_hash: str,
) -> str:
    if not prior:
        return VERSION_NEW
    if revision_dates_equal(prior.revision_date, revision_date):
        if prior.content_hash == content_hash:
            return VERSION_UNCHANGED
        return VERSION_CONTENT_CHANGE
    if revision_date or prior.revision_date:
        return VERSION_REVISED
    if prior.content_hash == content_hash:
        return VERSION_UNCHANGED
    return VERSION_CONTENT_CHANGE


def lookup_current_version(canonical_url: str) -> AuditSop | None:
    if not canonical_url:
        return None
    return (
        AuditSop.objects.filter(canonical_url=canonical_url, is_current=True)
        .order_by("-version_number", "-crawled_at")
        .first()
    )


def _metadata_snapshot(sop: AuditSop) -> dict[str, Any]:
    return {
        "title": sop.title,
        "platform": sop.platform,
        "effective_date": sop.effective_date,
        "revision_date": sop.revision_date,
        "lob": sop.lob or [],
        "audience": sop.audience or [],
        "step_count": sop.step_count,
        "decision_count": sop.decision_count,
        "code_count": sop.code_count,
    }


def _step_snapshot(sop: AuditSop) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for step in sop.steps.order_by("step_number"):
        decisions = []
        for dec in step.decisions.order_by("row_index"):
            decisions.append({
                "row_index": dec.row_index,
                "condition_if": dec.condition_if,
                "condition_and": dec.condition_and,
                "action_text": dec.action_text,
                "decision_type": dec.decision_type,
                "goto_step": dec.goto_step,
                "eob_codes": dec.eob_codes or [],
                "ex_codes": dec.ex_codes or [],
                "denial_codes": dec.denial_codes or [],
            })
        out[step.step_number] = {
            "question": step.question,
            "intro_text": step.intro_text,
            "is_terminal": step.is_terminal,
            "terminal_action": step.terminal_action,
            "decisions": decisions,
        }
    return out


def _precondition_snapshot(sop: AuditSop) -> list[dict[str, Any]]:
    return [{
        "order": pc.display_order,
        "category": pc.category,
        "label": pc.label,
        "content_text": pc.content_text,
        "llm_rules": pc.llm_rules or [],
    } for pc in sop.preconditions.order_by("display_order", "id")]


def _code_snapshot(sop: AuditSop) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for code in sop.codes.order_by("code_type", "code_value"):
        key = f"{code.code_type}:{code.code_value}"
        out[key] = {
            "code_type": code.code_type,
            "code_value": code.code_value,
            "description": code.description,
        }
    return out


def compute_sop_diff(from_sop: AuditSop, to_sop: AuditSop) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []

    old_meta, new_meta = _metadata_snapshot(from_sop), _metadata_snapshot(to_sop)
    for field in (
        "title", "platform", "effective_date", "revision_date",
        "step_count", "decision_count", "code_count",
    ):
        if old_meta.get(field) != new_meta.get(field):
            changes.append({
                "kind": "metadata",
                "field": field,
                "old": old_meta.get(field),
                "new": new_meta.get(field),
            })
    if old_meta.get("lob") != new_meta.get("lob"):
        changes.append({"kind": "metadata", "field": "lob",
                        "old": old_meta.get("lob"), "new": new_meta.get("lob")})
    if old_meta.get("audience") != new_meta.get("audience"):
        changes.append({"kind": "metadata", "field": "audience",
                        "old": old_meta.get("audience"), "new": new_meta.get("audience")})

    old_steps, new_steps = _step_snapshot(from_sop), _step_snapshot(to_sop)
    old_nums, new_nums = set(old_steps), set(new_steps)
    for num in sorted(old_nums - new_nums):
        changes.append({"kind": "step", "action": "removed", "step_number": num,
                        "old": old_steps[num]})
    for num in sorted(new_nums - old_nums):
        changes.append({"kind": "step", "action": "added", "step_number": num,
                        "new": new_steps[num]})
    for num in sorted(old_nums & new_nums):
        if old_steps[num] != new_steps[num]:
            changes.append({"kind": "step", "action": "modified", "step_number": num,
                            "old": old_steps[num], "new": new_steps[num]})

    old_pre, new_pre = _precondition_snapshot(from_sop), _precondition_snapshot(to_sop)
    if old_pre != new_pre:
        changes.append({"kind": "preconditions", "action": "modified",
                        "old_count": len(old_pre), "new_count": len(new_pre)})

    old_codes, new_codes = _code_snapshot(from_sop), _code_snapshot(to_sop)
    for key in sorted(set(old_codes) - set(new_codes)):
        changes.append({"kind": "code", "action": "removed", "key": key,
                        "old": old_codes[key]})
    for key in sorted(set(new_codes) - set(old_codes)):
        changes.append({"kind": "code", "action": "added", "key": key,
                        "new": new_codes[key]})
    for key in sorted(set(old_codes) & set(new_codes)):
        if old_codes[key] != new_codes[key]:
            changes.append({"kind": "code", "action": "modified", "key": key,
                            "old": old_codes[key], "new": new_codes[key]})

    summary = {
        "metadata_changes": sum(1 for c in changes if c["kind"] == "metadata"),
        "steps_added": sum(1 for c in changes if c["kind"] == "step" and c["action"] == "added"),
        "steps_removed": sum(1 for c in changes if c["kind"] == "step" and c["action"] == "removed"),
        "steps_modified": sum(1 for c in changes if c["kind"] == "step" and c["action"] == "modified"),
        "preconditions_changed": any(c["kind"] == "preconditions" for c in changes),
        "codes_added": sum(1 for c in changes if c["kind"] == "code" and c["action"] == "added"),
        "codes_removed": sum(1 for c in changes if c["kind"] == "code" and c["action"] == "removed"),
        "codes_modified": sum(1 for c in changes if c["kind"] == "code" and c["action"] == "modified"),
        "total_changes": len(changes),
    }
    return {"summary": summary, "changes": changes}


@transaction.atomic
def register_sop_version(
    sop_id: int,
    *,
    prior_sop_id: int | None,
    version_action: str,
    canonical_url: str,
    revision_date: str,
    auto_activate: bool = True,
) -> SopVersionDiff | None:
    """Link an ingested AuditSop row into the version chain and diff if needed."""
    sop = AuditSop.objects.select_for_update().get(pk=sop_id)
    canonical = normalize_canonical_url(canonical_url or sop.url)
    rev_norm = normalize_revision_date(revision_date or sop.revision_date)

    prior: AuditSop | None = None
    if prior_sop_id:
        prior = AuditSop.objects.filter(pk=prior_sop_id).first()

    doc, _ = SopDocument.objects.get_or_create(
        canonical_url=canonical,
        defaults={"title": sop.title, "latest_revision_date": rev_norm},
    )
    if sop.title and doc.title != sop.title:
        doc.title = sop.title

    next_version = 1
    if prior and prior.document_id:
        doc = prior.document
        next_version = prior.version_number + 1
    elif prior:
        next_version = prior.version_number + 1

    pending_status = ActivationStatus.PENDING_REVIEW
    active_status = ActivationStatus.ACTIVE
    superseded_status = ActivationStatus.SUPERSEDED

    if auto_activate:
        if prior and prior.document_id:
            AuditSop.objects.filter(document=doc, is_current=True).exclude(pk=sop.pk).update(
                is_current=False,
                activation_status=superseded_status,
            )
        elif prior:
            AuditSop.objects.filter(canonical_url=canonical, is_current=True).exclude(pk=sop.pk).update(
                is_current=False,
                activation_status=superseded_status,
            )
        sop.is_current = True
        sop.activation_status = active_status
        doc.latest_revision_date = rev_norm or doc.latest_revision_date
        doc.current_version = sop
        doc.save(update_fields=["title", "latest_revision_date", "current_version", "updated_at"])
    else:
        sop.is_current = False
        sop.activation_status = pending_status
        doc.save(update_fields=["title", "updated_at"])

    sop.document = doc
    sop.canonical_url = canonical
    sop.version_number = next_version
    sop.version_action = version_action
    sop.supersedes = prior
    sop.save(update_fields=[
        "document", "canonical_url", "version_number", "is_current",
        "version_action", "supersedes", "activation_status",
    ])

    if not prior or version_action in (VERSION_UNCHANGED, VERSION_NEW):
        return None

    payload = compute_sop_diff(prior, sop)
    diff, _ = SopVersionDiff.objects.update_or_create(
        from_sop=prior,
        to_sop=sop,
        defaults={
            "document": doc,
            "from_revision_date": prior.revision_date,
            "to_revision_date": sop.revision_date,
            "from_content_hash": prior.content_hash,
            "to_content_hash": sop.content_hash,
            "summary": payload["summary"],
            "changes": payload["changes"],
        },
    )
    log.info(
        "Registered SOP version  doc=%s  sop=%s  v%s  action=%s  auto_activate=%s  changes=%s",
        doc.id, sop.id, sop.version_number, version_action, auto_activate,
        payload["summary"].get("total_changes", 0),
    )
    return diff


@transaction.atomic
def activate_sop_version(sop_id: int, *, reviewed_by: str = "") -> AuditSop:
    """Promote a pending-review version to the live active SOP."""
    sop = AuditSop.objects.select_for_update().select_related("document").get(pk=sop_id)
    if sop.activation_status != ActivationStatus.PENDING_REVIEW:
        raise ValueError(
            f"SOP {sop_id} is not pending review (status={sop.activation_status})"
        )
    if not sop.document_id:
        raise ValueError(f"SOP {sop_id} is not linked to a SopDocument")

    doc = sop.document
    rev_norm = normalize_revision_date(sop.revision_date)

    AuditSop.objects.filter(document=doc, is_current=True).exclude(pk=sop.pk).update(
        is_current=False,
        activation_status=ActivationStatus.SUPERSEDED,
    )
    sop.is_current = True
    sop.activation_status = ActivationStatus.ACTIVE
    sop.save(update_fields=["is_current", "activation_status", "updated_at"])

    doc.current_version = sop
    doc.latest_revision_date = rev_norm or doc.latest_revision_date
    doc.save(update_fields=["current_version", "latest_revision_date", "updated_at"])

    log.info(
        "Activated SOP version  doc=%s  sop=%s  v%s  reviewer=%s",
        doc.id, sop.id, sop.version_number, reviewed_by or "unknown",
    )
    return sop


@transaction.atomic
def reject_sop_version(sop_id: int, *, reviewed_by: str = "", reason: str = "") -> AuditSop:
    """Reject a pending-review version without making it live."""
    sop = AuditSop.objects.select_for_update().get(pk=sop_id)
    if sop.activation_status != ActivationStatus.PENDING_REVIEW:
        raise ValueError(
            f"SOP {sop_id} is not pending review (status={sop.activation_status})"
        )

    sop.is_current = False
    sop.activation_status = ActivationStatus.REJECTED
    sop.save(update_fields=["is_current", "activation_status", "updated_at"])

    log.info(
        "Rejected SOP version  sop=%s  v%s  reviewer=%s  reason=%s",
        sop.id, sop.version_number, reviewed_by or "unknown", reason[:200],
    )
    return sop
