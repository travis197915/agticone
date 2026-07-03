"""Whether an AuditSop version is approved for workflow binding and execution."""
from __future__ import annotations

from typing import Any

from sop_ingestion.models import ActivationStatus, AuditSop


def workflow_binding_compliance(workflow) -> dict[str, Any]:
    """Summarize whether canvas rule bindings use approved active SOP versions."""
    try:
        from agent_tools.models import NodeRuleBinding
    except Exception:
        return {
            "binding_count": 0,
            "unapproved_binding_count": 0,
            "has_unapproved_bindings": False,
            "unapproved_sop_ids": [],
        }

    unapproved_sop_ids: set[int] = set()
    binding_count = 0
    for row in (
        NodeRuleBinding.objects.filter(
            shape__workbench__work_area__workflow=workflow,
        )
        .select_related("sop", "sop__document", "sop__document__current_version")
    ):
        binding_count += 1
        if not sop_approval_meta(row.sop)["is_approved"]:
            unapproved_sop_ids.add(row.sop_id)

    unapproved = len(unapproved_sop_ids)
    return {
        "binding_count": binding_count,
        "unapproved_binding_count": unapproved,
        "has_unapproved_bindings": unapproved > 0,
        "unapproved_sop_ids": sorted(unapproved_sop_ids),
    }


def sop_approval_meta(sop: AuditSop | None) -> dict[str, Any]:
    """Return approval flags for one ingested SOP version."""
    if sop is None:
        return {
            "sop_id": None,
            "document_id": None,
            "activation_status": None,
            "is_current": False,
            "current_sop_id": None,
            "is_approved": False,
            "approval_issue": "sop_not_found",
        }

    doc = sop.document
    current_sop_id = doc.current_version_id if doc else None
    issues: list[str] = []

    if sop.activation_status != ActivationStatus.ACTIVE:
        issues.append(f"activation_status={sop.activation_status}")
    if not sop.is_current:
        issues.append("not_current")
    if doc and current_sop_id and sop.id != current_sop_id:
        issues.append("superseded_by_active_version")

    return {
        "sop_id": sop.id,
        "document_id": sop.document_id,
        "activation_status": sop.activation_status,
        "is_current": sop.is_current,
        "version_number": sop.version_number,
        "current_sop_id": current_sop_id,
        "is_approved": not issues,
        "approval_issue": "; ".join(issues) if issues else None,
    }


def require_approved_sop(sop: AuditSop, *, context: str = "attach") -> None:
    """Raise ValidationError if this SOP version must not be bound to a workflow shape."""
    from rest_framework.exceptions import ValidationError

    meta = sop_approval_meta(sop)
    if meta["is_approved"]:
        return
    raise ValidationError({
        "sop_id": sop.id,
        "approval_issue": meta["approval_issue"],
        "detail": (
            f"Cannot {context} rules from SOP {sop.id}: {meta['approval_issue']}. "
            "Activate the pending version via POST /api/ingest/sops/<sop_id>/activate/ "
            "or re-bind to the current active version."
        ),
    })
