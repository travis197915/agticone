"""URL routes for claim processing snapshots.

Mounted under ``/api/claims/`` from ``sop_backend/urls.py``.
"""
from __future__ import annotations

from django.urls import path

from .views import ClaimProcessingView

app_name = "execution_claims"

urlpatterns = [
    path("<str:claim_id>/processing/",
         ClaimProcessingView.as_view(), name="claim-processing"),
]
