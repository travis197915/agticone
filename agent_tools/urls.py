"""
URL routes for the agent_tools registry + invoke surface.

Mounted at ``/api/agent-tools/`` from :mod:`sop_backend.urls`. The mock
upstream routes live in :mod:`agent_tools.mock.urls` and are mounted
separately under ``/api/mocks/``.
"""
from __future__ import annotations

from django.urls import path

from .views import (FieldMappingDetailView, FieldMappingListView,
                    FieldMappingMetaView, McpServerDetailView,
                    McpServerListView, McpServerTestView, OntologyDetailView,
                    OntologyListView, ToolAnalyzeView, ToolContextView,
                    ToolDetailView, ToolInvokeView, ToolListView)

app_name = "agent_tools"

urlpatterns = [
    path("", ToolListView.as_view(), name="tool-list"),
    # Config CRUD — declared BEFORE the ``<str:name>/`` catch-all so these
    # explicit paths are not shadowed by the tool-detail route.
    path("field-mappings/", FieldMappingListView.as_view(), name="field-mapping-list"),
    path("field-mappings/meta/", FieldMappingMetaView.as_view(), name="field-mapping-meta"),
    path("field-mappings/<uuid:pk>/", FieldMappingDetailView.as_view(), name="field-mapping-detail"),
    path("claim-ontology/", OntologyListView.as_view(), name="ontology-list"),
    path("claim-ontology/<uuid:pk>/", OntologyDetailView.as_view(), name="ontology-detail"),
    path("mcp-servers/", McpServerListView.as_view(), name="mcp-server-list"),
    path("mcp-servers/<uuid:pk>/", McpServerDetailView.as_view(), name="mcp-server-detail"),
    path("mcp-servers/<uuid:pk>/test/", McpServerTestView.as_view(), name="mcp-server-test"),
    path("<str:name>/", ToolDetailView.as_view(), name="tool-detail"),
    path("<str:name>/invoke", ToolInvokeView.as_view(), name="tool-invoke"),
    path("<str:name>/analyze", ToolAnalyzeView.as_view(), name="tool-analyze"),
    path("<str:name>/context", ToolContextView.as_view(), name="tool-context"),
]
