"""URL routes for the execution agent.

Mounted under ``/api/execute/`` from sop_backend/urls.py.
"""
from __future__ import annotations

from django.urls import path

from .views import (BatchDetailView, BatchEventsView, BatchLatestView,
                     RunBatchAsyncView, RunBatchView, RunDetailView, RunListView,
                     RunNodesView, RunReviewApproveView, RunReviewRejectView,
                     RunReviewReleaseView, RunReviewStatusView)

app_name = "execution_app"

urlpatterns = [
    path("workflows/<uuid:workflow_id>/run-batch/",
         RunBatchView.as_view(), name="run-batch"),
    path("workflows/<uuid:workflow_id>/run-batch-async/",
         RunBatchAsyncView.as_view(), name="run-batch-async"),
    path("batches/latest/",
         BatchLatestView.as_view(), name="batch-latest"),
    path("batches/<uuid:batch_id>/",
         BatchDetailView.as_view(), name="batch-detail"),
    path("batches/<uuid:batch_id>/events/",
         BatchEventsView.as_view(), name="batch-events"),
    path("runs/",
         RunListView.as_view(), name="run-list"),
    path("runs/<uuid:run_id>/",
         RunDetailView.as_view(), name="run-detail"),
    path("runs/<uuid:run_id>/review-status/",
         RunReviewStatusView.as_view(), name="run-review-status"),
    path("runs/<uuid:run_id>/review/approve/",
         RunReviewApproveView.as_view(), name="run-review-approve"),
    path("runs/<uuid:run_id>/review/reject/",
         RunReviewRejectView.as_view(), name="run-review-reject"),
    path("runs/<uuid:run_id>/review/release/",
         RunReviewReleaseView.as_view(), name="run-review-release"),
    path("runs/<uuid:run_id>/nodes/",
         RunNodesView.as_view(), name="run-nodes"),
]
