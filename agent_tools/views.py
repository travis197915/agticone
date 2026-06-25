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

from .models import ClaimOntologyField, SopFieldMapping, Tool
from .serializers import (SYSTEM_LABELS, ClaimOntologyFieldSerializer,
                          SopFieldMappingSerializer, ToolSerializer)

logger = logging.getLogger(__name__)


class ToolListView(APIView):
    """``GET /api/agent-tools/`` — flat list of active tools."""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        qs = Tool.objects.filter(is_active=True).order_by("display_name", "name")
        return Response(ToolSerializer(qs, many=True).data)


class FieldMappingMetaView(APIView):
    """``GET /api/agent-tools/field-mappings/meta/`` — UI helper metadata
    (the ordered source systems + friendly labels)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        return Response({"systems": SYSTEM_LABELS})


class FieldMappingListView(APIView):
    """``GET`` list / ``POST`` create SOP field mappings."""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        qs = SopFieldMapping.objects.all()
        if request.query_params.get("active_only") in ("1", "true", "True"):
            qs = qs.filter(is_active=True)
        return Response(SopFieldMappingSerializer(qs, many=True).data)

    def post(self, request, *args, **kwargs):
        ser = SopFieldMappingSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data, status=status.HTTP_201_CREATED)


class FieldMappingDetailView(APIView):
    """``GET`` / ``PUT``/``PATCH`` / ``DELETE`` one SOP field mapping."""

    permission_classes = [IsAuthenticated]

    def _get(self, pk):
        return SopFieldMapping.objects.filter(pk=pk).first()

    def get(self, request, pk, *args, **kwargs):
        obj = self._get(pk)
        if not obj:
            return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(SopFieldMappingSerializer(obj).data)

    def put(self, request, pk, *args, **kwargs):
        return self._update(request, pk, partial=False)

    def patch(self, request, pk, *args, **kwargs):
        return self._update(request, pk, partial=True)

    def _update(self, request, pk, *, partial):
        obj = self._get(pk)
        if not obj:
            return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
        ser = SopFieldMappingSerializer(obj, data=request.data, partial=partial)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    def delete(self, request, pk, *args, **kwargs):
        obj = self._get(pk)
        if not obj:
            return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class OntologyListView(APIView):
    """``GET`` list / ``POST`` create claim-ontology fields."""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        qs = ClaimOntologyField.objects.all()
        ns = request.query_params.get("namespace")
        if ns:
            qs = qs.filter(namespace=ns)
        if request.query_params.get("active_only") in ("1", "true", "True"):
            qs = qs.filter(is_active=True)
        return Response(ClaimOntologyFieldSerializer(qs, many=True).data)

    def post(self, request, *args, **kwargs):
        ser = ClaimOntologyFieldSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data, status=status.HTTP_201_CREATED)


class OntologyDetailView(APIView):
    """``GET`` / ``PUT``/``PATCH`` / ``DELETE`` one claim-ontology field."""

    permission_classes = [IsAuthenticated]

    def _get(self, pk):
        return ClaimOntologyField.objects.filter(pk=pk).first()

    def get(self, request, pk, *args, **kwargs):
        obj = self._get(pk)
        if not obj:
            return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(ClaimOntologyFieldSerializer(obj).data)

    def put(self, request, pk, *args, **kwargs):
        return self._update(request, pk, partial=False)

    def patch(self, request, pk, *args, **kwargs):
        return self._update(request, pk, partial=True)

    def _update(self, request, pk, *, partial):
        obj = self._get(pk)
        if not obj:
            return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
        ser = ClaimOntologyFieldSerializer(obj, data=request.data, partial=partial)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    def delete(self, request, pk, *args, **kwargs):
        obj = self._get(pk)
        if not obj:
            return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


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
