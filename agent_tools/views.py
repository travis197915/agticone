"""
REST views for ``agent_tools``.

Three endpoints, all under ``/api/agent-tools/``:

* ``GET  /``                — list active :class:`~agent_tools.models.Tool`
                              rows.
* ``GET  /{name}/``         — full descriptor for one tool (Pydantic JSON
                              schema for inputs, kind discriminator,
                              invoke URL).
* ``POST /{name}/invoke``   — execute the tool through the minimal
                              LangGraph ``single_tool_graph`` and return
                              ``{"ok": bool, "result": ..., "error": ...}``.
"""
from __future__ import annotations

import logging
from typing import Any

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Tool
from .serializers import ToolSerializer

logger = logging.getLogger(__name__)


class ToolListView(APIView):
    """``GET /api/agent-tools/`` — flat list of active tools."""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        qs = Tool.objects.filter(is_active=True).order_by("display_name", "name")
        return Response(ToolSerializer(qs, many=True).data)


class ToolDetailView(APIView):
    """``GET /api/agent-tools/{name}/`` — single tool with full args schema."""

    permission_classes = [IsAuthenticated]

    def get(self, request, name: str, *args, **kwargs):
        try:
            tool = Tool.objects.get(name=name)
        except Tool.DoesNotExist:
            return Response(
                {"error": f"unknown tool '{name}'"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(ToolSerializer(tool).data)


class ToolInvokeView(APIView):
    """``POST /api/agent-tools/{name}/invoke`` — run a tool through LangGraph."""

    permission_classes = [IsAuthenticated]

    def post(self, request, name: str, *args, **kwargs):
        try:
            tool = Tool.objects.get(name=name, is_active=True)
        except Tool.DoesNotExist:
            return Response(
                {"ok": False, "error": f"unknown tool '{name}'"},
                status=status.HTTP_404_NOT_FOUND,
            )

        body: dict[str, Any] = request.data if isinstance(request.data, dict) else {}
        args: dict[str, Any] = body.get("args") or {}
        if not isinstance(args, dict):
            return Response(
                {"ok": False, "error": "'args' must be a JSON object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Lazy import to keep the URL conf import cheap and to avoid pulling
        # langgraph during Django startup.
        from .graphs.single_tool_graph import run_tool

        try:
            result = run_tool(tool.name, args)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("tool '%s' invoke failed", tool.name)
            return Response(
                {"ok": False, "error": str(exc), "tool": tool.name},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            {"ok": True, "tool": tool.name, "result": result},
            status=status.HTTP_200_OK,
        )
