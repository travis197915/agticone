"""URL routes for claim processing snapshots.

Mounted under ``/api/claims/`` from ``sop_backend/urls.py``.
"""
from __future__ import annotations

from django.urls import path

from .views import (ClaimAgentsView, ClaimProcessingView, ClaimSummaryView,
                     ClaimTraceView)

app_name = "execution_claims"

urlpatterns = [
    path("<str:claim_id>/summary/",
         ClaimSummaryView.as_view(), name="claim-summary"),
    path("<str:claim_id>/agents/",
         ClaimAgentsView.as_view(), name="claim-agents"),
    path("<str:claim_id>/processing/",
         ClaimProcessingView.as_view(), name="claim-processing"),
    path("<str:claim_id>/trace/",
         ClaimTraceView.as_view(kind="trace"), name="claim-trace"),
    path("<str:claim_id>/explainability/",
         ClaimTraceView.as_view(kind="explainability"), name="claim-explainability"),
]
