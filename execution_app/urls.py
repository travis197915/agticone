"""URL routes for the execution agent.

Mounted under ``/api/execute/`` from sop_backend/urls.py.
"""
from __future__ import annotations

from django.urls import path

from .views import (BatchDetailView, BatchEventsView, RunBatchAsyncView,
                     RunBatchView, RunDetailView, RunNodesView)

app_name = "execution_app"

urlpatterns = [
    path("workflows/<uuid:workflow_id>/run-batch/",
         RunBatchView.as_view(), name="run-batch"),
    path("workflows/<uuid:workflow_id>/run-batch-async/",
         RunBatchAsyncView.as_view(), name="run-batch-async"),
    path("batches/<uuid:batch_id>/",
         BatchDetailView.as_view(), name="batch-detail"),
    path("batches/<uuid:batch_id>/events/",
         BatchEventsView.as_view(), name="batch-events"),
    path("runs/<uuid:run_id>/",
         RunDetailView.as_view(), name="run-detail"),
    path("runs/<uuid:run_id>/nodes/",
         RunNodesView.as_view(), name="run-nodes"),
]
