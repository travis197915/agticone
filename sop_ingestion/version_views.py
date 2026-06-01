"""REST endpoints for SOP document versions and diffs."""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ActivationStatus, AuditSop, SopDocument, SopVersionDiff
from .services.versioning import (
    VERSION_UNCHANGED,
    activate_sop_version,
    reject_sop_version,
)


class SopDocumentVersionsView(APIView):
    """List all ingested versions for a stable SOP document identity."""

    def get(self, request: Request, document_id: int) -> Response:
        doc = get_object_or_404(SopDocument, pk=document_id)
        versions = doc.versions.order_by("-version_number", "-crawled_at")
        pending = versions.filter(activation_status=ActivationStatus.PENDING_REVIEW)
        return Response({
            "document_id": doc.id,
            "canonical_url": doc.canonical_url,
            "title": doc.title,
            "latest_revision_date": doc.latest_revision_date,
            "current_sop_id": doc.current_version_id,
            "pending_review_count": pending.count(),
            "pending_review_sop_ids": list(pending.values_list("id", flat=True)),
            "versions": [{
                "sop_id": v.id,
                "job_id": str(v.job_id),
                "version_number": v.version_number,
                "is_current": v.is_current,
                "activation_status": v.activation_status,
                "version_action": v.version_action,
                "revision_date": v.revision_date,
                "effective_date": v.effective_date,
                "content_hash": v.content_hash,
                "supersedes_sop_id": v.supersedes_id,
                "crawled_at": v.crawled_at,
            } for v in versions],
        })


class SopDocumentRevisionCheckView(APIView):
    """Probe one tracked SOP document for revision/content drift.

    POST /api/ingest/documents/<document_id>/revision-check/
      ?dry_run=1   — probe only; do not queue re-ingestion (default: false)
      ?async=1     — queue Celery task (default: false — runs inline)
      ?force=1     — fail a blocking queued/running job and queue re-ingest

    When drift is found and re-ingestion is queued, the new version is saved
    as ``pending_review`` and does **not** replace the active SOP until a
    human calls ``POST /api/ingest/sops/<sop_id>/activate/``.
    """

    def post(self, request: Request, document_id: int) -> Response:
        dry_run = request.query_params.get("dry_run", "").lower() in {"1", "true", "yes"}
        use_async = request.query_params.get("async", "").lower() in {"1", "true", "yes"}
        force = request.query_params.get("force", "").lower() in {"1", "true", "yes"}
        dispatch = not dry_run

        if use_async:
            if not SopDocument.objects.filter(pk=document_id).exists():
                return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
            from .tasks import check_sop_revision

            task = check_sop_revision.delay(document_id, dispatch=dispatch, force=force)
            return Response(
                {
                    "document_id": document_id,
                    "dispatched": True,
                    "dry_run": dry_run,
                    "force": force,
                    "celery_task_id": task.id,
                },
                status=status.HTTP_202_ACCEPTED,
            )

        from .services.revision_scheduler import check_tracked_sop, tracked_sop_from_document

        doc = get_object_or_404(
            SopDocument.objects.select_related("current_version", "current_version__job"),
            pk=document_id,
        )
        result = check_tracked_sop(
            tracked_sop_from_document(doc),
            dispatch=dispatch,
            force=force,
        )
        if result.get("error"):
            return Response(result, status=status.HTTP_502_BAD_GATEWAY)
        if "has_deviation" not in result:
            result["has_deviation"] = result.get("version_action") != VERSION_UNCHANGED
        return Response(result, status=status.HTTP_200_OK)


class SopVersionActivateView(APIView):
    """Promote a pending-review ingested version to the live active SOP.

    POST /api/ingest/sops/<sop_id>/activate/
    Body (optional): ``{ "reviewed_by": "auditor@example.com" }``
    """

    def post(self, request: Request, sop_id: int) -> Response:
        reviewed_by = str(request.data.get("reviewed_by") or "").strip()
        try:
            sop = activate_sop_version(sop_id, reviewed_by=reviewed_by)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({
            "sop_id": sop.id,
            "document_id": sop.document_id,
            "version_number": sop.version_number,
            "is_current": sop.is_current,
            "activation_status": sop.activation_status,
            "revision_date": sop.revision_date,
        })


class SopVersionRejectView(APIView):
    """Reject a pending-review version without activating it.

    POST /api/ingest/sops/<sop_id>/reject/
    Body (optional): ``{ "reviewed_by": "...", "reason": "..." }``
    """

    def post(self, request: Request, sop_id: int) -> Response:
        reviewed_by = str(request.data.get("reviewed_by") or "").strip()
        reason = str(request.data.get("reason") or "").strip()
        try:
            sop = reject_sop_version(sop_id, reviewed_by=reviewed_by, reason=reason)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({
            "sop_id": sop.id,
            "document_id": sop.document_id,
            "version_number": sop.version_number,
            "is_current": sop.is_current,
            "activation_status": sop.activation_status,
        })


class RevisionCheckView(APIView):
    """Trigger a revision-date probe across all tracked SOP documents.

    POST /api/ingest/revision-check/
      ?async=1   — queue Celery task (default)
      ?dry_run=1 — probe only, do not queue ingestion jobs
    """

    def post(self, request: Request) -> Response:
        dry_run = request.query_params.get("dry_run", "").lower() in {"1", "true", "yes"}
        use_async = request.query_params.get("async", "1").lower() in {"1", "true", "yes"}

        if use_async:
            from .tasks import check_all_sop_revisions

            if dry_run:
                from .services.revision_scheduler import run_revision_check_batch
                result = run_revision_check_batch(dispatch=False)
                return Response(result, status=status.HTTP_200_OK)

            task = check_all_sop_revisions.delay()
            return Response(
                {
                    "dispatched": True,
                    "dry_run": False,
                    "celery_task_id": task.id,
                },
                status=status.HTTP_202_ACCEPTED,
            )

        from .services.revision_scheduler import run_revision_check_batch

        result = run_revision_check_batch(dispatch=not dry_run)
        return Response(result, status=status.HTTP_200_OK)


class SopVersionDiffView(APIView):
    """Return the stored diff for a specific ingested SOP version."""

    def get(self, request: Request, sop_id: int) -> Response:
        sop = get_object_or_404(AuditSop, pk=sop_id)
        diff = (
            SopVersionDiff.objects.filter(to_sop=sop)
            .select_related("from_sop", "document")
            .order_by("-computed_at")
            .first()
        )
        if not diff:
            return Response({
                "sop_id": sop.id,
                "document_id": sop.document_id,
                "version_number": sop.version_number,
                "version_action": sop.version_action,
                "has_diff": False,
                "summary": {},
                "changes": [],
            })
        return Response({
            "sop_id": sop.id,
            "document_id": diff.document_id,
            "from_sop_id": diff.from_sop_id,
            "to_sop_id": diff.to_sop_id,
            "from_revision_date": diff.from_revision_date,
            "to_revision_date": diff.to_revision_date,
            "from_content_hash": diff.from_content_hash,
            "to_content_hash": diff.to_content_hash,
            "version_action": sop.version_action,
            "has_diff": True,
            "summary": diff.summary,
            "changes": diff.changes,
            "computed_at": diff.computed_at,
        })
