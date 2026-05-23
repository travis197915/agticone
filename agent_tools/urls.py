"""
URL routes for the agent_tools registry + invoke surface.

Mounted at ``/api/agent-tools/`` from :mod:`sop_backend.urls`. The mock
upstream routes live in :mod:`agent_tools.mock.urls` and are mounted
separately under ``/api/mocks/``.
"""
from __future__ import annotations

from django.urls import path

from .views import ToolDetailView, ToolInvokeView, ToolListView

app_name = "agent_tools"

urlpatterns = [
    path("", ToolListView.as_view(), name="tool-list"),
    path("<str:name>/", ToolDetailView.as_view(), name="tool-detail"),
    path("<str:name>/invoke", ToolInvokeView.as_view(), name="tool-invoke"),
]
