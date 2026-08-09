"""REST surface for auditor-authored rule edits.

    POST /api/ingest/rules/<decision_id>/propose/   propose an edit
    GET  /api/ingest/rules/pending/?sop_id=         badge rules with open edits
    GET  /api/ingest/rule-changesets/               review inbox (the bell)
    GET  /api/ingest/rule-changesets/<id>/          diff detail (the modal)
    POST /api/ingest/rule-changesets/<id>/approve/  apply the batch
    POST /api/ingest/rule-changesets/<id>/reject/   close without applying

Authentication is the project default (``CorebackendJWTAuthentication`` +
``IsAuthenticated``). Approve/reject additionally require the
``sop:approve-changes`` permission; ADMIN satisfies this through the wildcard
grant, so it works before the relay seeds the explicit permission row.
"""
from __future__ import annotations

from typing import Any

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from builder.auth import HasPermission

from .models import AuditDecision, ChangeSetStatus, RuleChangeSet
from .services.rule_changes import (
    ChangeSetClosed,
    ChangeSetMoved,
    ChangeSetStale,
    NoEffectiveChange,
    RuleChangeError,
    approve_change_set,
    change_set_payload,
    list_change_sets,
    pending_proposals_for_sop,
    propose_rule_change,
    reject_change_set,
    workflow_names_by_sop,
)

APPROVE_PERMISSION = "sop:approve-changes"

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _actor(request: Request) -> str:
    """Identify the acting user for authorship / review attribution.

    Prefers the JWT identity; falls back to an explicit body field so the
    endpoints stay usable from scripts and tests, matching the ``reviewed_by``
    convention already used by the SOP version activate/reject views.
    """
    user = getattr(request, "user", None)
    email = str(getattr(user, "email", "") or "").strip()
    if email:
        return email
    uid = str(getattr(user, "id", "") or "").strip()
    if uid:
        return uid
    data = request.data if isinstance(getattr(request, "data", None), dict) else {}
    return str(data.get("reviewed_by") or data.get("author") or "").strip()


def _error_response(exc: RuleChangeError) -> Response:
    """Map a service error to its HTTP shape.

    ``NoEffectiveChange`` is a malformed request (nothing to do); the rest are
    genuine conflicts the caller resolves by refreshing.
    """
    code = (
        status.HTTP_400_BAD_REQUEST
        if isinstance(exc, NoEffectiveChange)
        else status.HTTP_409_CONFLICT
    )
    return Response({"error": exc.code, "detail": str(exc)}, status=code)


class RuleProposeView(APIView):
    """POST /api/ingest/rules/<decision_id>/propose/

    Body: ``{"fields": {"condition_if": "...", "action_text": "..."}}``

    Records the edit as a pending proposal inside the author's open change set
    for that SOP. The canonical rule is untouched until the batch is approved.
    """

    def post(self, request: Request, decision_id: int) -> Response:
        decision = get_object_or_404(
            AuditDecision.objects.select_related("step", "step__sop"),
            pk=decision_id,
        )
        payload: dict[str, Any] = request.data if isinstance(request.data, dict) else {}
        fields = payload.get("fields")
        if not isinstance(fields, dict) or not fields:
            return Response(
                {"error": "missing_fields",
                 "detail": "Body must include a non-empty 'fields' object."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            proposal = propose_rule_change(
                decision=decision, fields=fields, author=_actor(request),
            )
        except RuleChangeError as exc:
            return _error_response(exc)

        change_set = proposal.changeset
        return Response(
            {
                "changeset_id": change_set.id,
                "proposal_id": proposal.id,
                "status": change_set.status,
                "display_rule_id": proposal.display_rule_id,
                "fields_changed": sorted((proposal.proposed_fields or {}).keys()),
                "base_revision": proposal.base_revision,
                "from_version": change_set.base_version,
                "to_version": change_set.base_version + 1,
            },
            status=status.HTTP_201_CREATED,
        )


class PendingRuleProposalsView(APIView):
    """GET /api/ingest/rules/pending/?sop_id=<id>

    Map of ``decision_id`` → open proposal, so the editing UI can badge rules
    that already carry an unreviewed edit and render the proposed text.
    """

    def get(self, request: Request) -> Response:
        raw = (request.query_params.get("sop_id") or "").strip()
        if not raw.isdigit():
            return Response(
                {"error": "missing_sop_id", "detail": "Query param 'sop_id' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"sop_id": int(raw), "pending": pending_proposals_for_sop(int(raw))})


class RuleChangeSetListView(APIView):
    """GET /api/ingest/rule-changesets/?status=open&sop_id=&created_by=&limit=

    Drives the notification bell and its dropdown.
    """

    def get(self, request: Request) -> Response:
        params = request.query_params
        status_filter = (params.get("status") or ChangeSetStatus.OPEN).strip()
        if status_filter.lower() in ("", "all"):
            status_filter = None

        sop_raw = (params.get("sop_id") or "").strip()
        created_by = (params.get("created_by") or "").strip() or None

        try:
            limit = int(params.get("limit") or _DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT
        limit = max(1, min(limit, _MAX_LIMIT))

        qs = list_change_sets(
            status=status_filter,
            sop_id=int(sop_raw) if sop_raw.isdigit() else None,
            created_by=created_by,
            # Lets the claims frontend show a workflow only its own pending
            # SOP updates, rather than every batch on the same document.
            workflow_id=(params.get("workflow_id") or "").strip() or None,
            source=(params.get("source") or "").strip() or None,
        )
        change_sets = list(qs[:limit])
        names = workflow_names_by_sop({cs.sop_id for cs in change_sets})
        return Response({
            "count": len(change_sets),
            "results": [
                change_set_payload(cs, workflow_names=names.get(cs.sop_id, []))
                for cs in change_sets
            ],
        })


class RuleChangeSetDetailView(APIView):
    """GET /api/ingest/rule-changesets/<id>/ — the review modal's payload."""

    def get(self, request: Request, changeset_id: int) -> Response:
        change_set = get_object_or_404(
            RuleChangeSet.objects.select_related("sop", "to_sop", "workflow"),
            pk=changeset_id,
        )
        names = workflow_names_by_sop([change_set.sop_id])
        return Response(change_set_payload(
            change_set,
            include_proposals=True,
            workflow_names=names.get(change_set.sop_id, []),
        ))


class RuleChangeSetApproveView(APIView):
    """POST /api/ingest/rule-changesets/<id>/approve/

    Body: ``{"proposal_ids": [11, 12, 13]}`` — exactly the proposals the
    reviewer saw. A mismatch means the author added or removed a rule while the
    modal was open, and returns 409 ``changeset_moved``.
    """

    permission_classes = [HasPermission(APPROVE_PERMISSION)]

    def post(self, request: Request, changeset_id: int) -> Response:
        change_set = get_object_or_404(
            RuleChangeSet.objects.select_related("sop", "to_sop", "workflow"),
            pk=changeset_id,
        )
        payload: dict[str, Any] = request.data if isinstance(request.data, dict) else {}
        raw_ids = payload.get("proposal_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return Response(
                {"error": "missing_proposal_ids",
                 "detail": "Body must include a non-empty 'proposal_ids' array."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            proposal_ids = [int(i) for i in raw_ids]
        except (TypeError, ValueError):
            return Response(
                {"error": "invalid_proposal_ids",
                 "detail": "'proposal_ids' must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = approve_change_set(
                change_set, proposal_ids=proposal_ids, reviewer=_actor(request),
            )
        except RuleChangeError as exc:
            return _error_response(exc)

        return Response({
            "changeset_id": result.get("changeset_id"),
            "sop_id": result.get("sop_id"),
            "resulting_version": result.get("resulting_version"),
            "applied": result.get("applied", []),
            "skipped": result.get("skipped", []),
            "batch_id": result.get("batch_id", ""),
        })


class RuleChangeSetRejectView(APIView):
    """POST /api/ingest/rule-changesets/<id>/reject/

    Body (optional): ``{"note": "why"}``. Closes the batch without applying.
    The proposals are dead; the author re-edits from scratch.
    """

    permission_classes = [HasPermission(APPROVE_PERMISSION)]

    def post(self, request: Request, changeset_id: int) -> Response:
        change_set = get_object_or_404(RuleChangeSet, pk=changeset_id)
        payload: dict[str, Any] = request.data if isinstance(request.data, dict) else {}
        try:
            change_set = reject_change_set(
                change_set,
                reviewer=_actor(request),
                note=str(payload.get("note") or ""),
            )
        except RuleChangeError as exc:
            return _error_response(exc)
        return Response({
            "changeset_id": change_set.id,
            "status": change_set.status,
            "reviewed_by": change_set.reviewed_by,
            "reviewed_at": change_set.reviewed_at,
            "review_note": change_set.review_note,
        })
