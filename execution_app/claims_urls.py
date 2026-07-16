"""URL routes for claim processing snapshots.

Mounted under ``/api/claims/`` from ``sop_backend/urls.py``.
"""
from __future__ import annotations

from django.urls import path

from .views import (ClaimAgentsView, ClaimProcessingView, ClaimReviewApproveView,
                     ClaimReviewRejectView, ClaimReviewReleaseView, ClaimReviewStatusView,
                     ClaimSummaryView, ClaimTraceView)

app_name = "execution_claims"

urlpatterns = [
    path("<str:claim_id>/summary/",
         ClaimSummaryView.as_view(), name="claim-summary"),
    path("<str:claim_id>/agents/",
         ClaimAgentsView.as_view(), name="claim-agents"),
    path("<str:claim_id>/processing/",
         ClaimProcessingView.as_view(), name="claim-processing"),
    path("<str:claim_id>/review-status/",
         ClaimReviewStatusView.as_view(), name="claim-review-status"),
    path("<str:claim_id>/review/approve/",
         ClaimReviewApproveView.as_view(), name="claim-review-approve"),
    path("<str:claim_id>/review/reject/",
         ClaimReviewRejectView.as_view(), name="claim-review-reject"),
    path("<str:claim_id>/review/release/",
         ClaimReviewReleaseView.as_view(), name="claim-review-release"),
    path("<str:claim_id>/trace/",
         ClaimTraceView.as_view(kind="trace"), name="claim-trace"),
    path("<str:claim_id>/explainability/",
         ClaimTraceView.as_view(kind="explainability"), name="claim-explainability"),
]
