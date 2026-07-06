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

from .models import (ClaimOntologyField, McpServerConfig, McpToolContext,
                     SopFieldMapping, Tool)
from .serializers import (SYSTEM_LABELS, ClaimOntologyFieldSerializer,
                          McpServerConfigSerializer, McpToolContextSerializer,
                          SopFieldMappingSerializer, ToolSerializer,
                          ToolWriteSerializer)

logger = logging.getLogger(__name__)


class ToolListView(APIView):
    """``GET`` list / ``POST`` create tool-call rows.

    ``GET`` returns active tools by default; pass ``?all=1`` to include inactive
    rows (the config UI needs them to toggle activation).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        qs = Tool.objects.all().order_by("display_name", "name")
        if request.query_params.get("all") not in ("1", "true", "True"):
            qs = qs.filter(is_active=True)

        # Server-side search across name / display name / MCP path so paging is
        # correct over the full set (a client can't filter a single page).
        search = (request.query_params.get("search") or "").strip()
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(display_name__icontains=search)
                | Q(metadata__mcp_path__icontains=search)
            )

        # Pagination is opt-in: only when ``?page`` is present do we return the
        # envelope. Without it we keep the bare array so existing consumers
        # (workflow builder palette, attach picker, …) are unaffected.
        page_param = request.query_params.get("page")
        if page_param is None:
            return Response(ToolSerializer(qs, many=True).data)

        from django.core.paginator import Paginator

        try:
            page_num = max(1, int(page_param))
        except (TypeError, ValueError):
            page_num = 1
        try:
            page_size = int(request.query_params.get("page_size") or 8)
        except (TypeError, ValueError):
            page_size = 8
        page_size = max(1, min(page_size, 100))

        paginator = Paginator(qs, page_size)
        page_obj = paginator.get_page(page_num)
        return Response({
            "results": ToolSerializer(page_obj.object_list, many=True).data,
            "page": page_obj.number,
            "page_size": page_size,
            "total_count": paginator.count,
            "total_pages": paginator.num_pages,
            "has_next": page_obj.has_next(),
            "has_prev": page_obj.has_previous(),
        })

    def post(self, request, *args, **kwargs):
        ser = ToolWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data, status=status.HTTP_201_CREATED)


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
    """``GET`` / ``PUT`` / ``PATCH`` / ``DELETE`` a single tool by ``name``."""

    permission_classes = [IsAuthenticated]

    def _get(self, name: str):
        return Tool.objects.filter(name=name).first()

    def get(self, request, name: str, *args, **kwargs):
        tool = self._get(name)
        if not tool:
            return Response(
                {"error": f"unknown tool '{name}'"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(ToolSerializer(tool).data)

    def put(self, request, name: str, *args, **kwargs):
        return self._update(request, name, partial=False)

    def patch(self, request, name: str, *args, **kwargs):
        return self._update(request, name, partial=True)

    def _update(self, request, name: str, *, partial):
        tool = self._get(name)
        if not tool:
            return Response({"error": f"unknown tool '{name}'"},
                            status=status.HTTP_404_NOT_FOUND)
        ser = ToolWriteSerializer(tool, data=request.data, partial=partial)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    def delete(self, request, name: str, *args, **kwargs):
        tool = self._get(name)
        if not tool:
            return Response({"error": f"unknown tool '{name}'"},
                            status=status.HTTP_404_NOT_FOUND)
        tool.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class McpServerListView(APIView):
    """``GET`` list / ``POST`` create external MCP/REST server configs.

    ``GET`` also returns ``runtime_config_source`` (``env`` | ``db`` | ``none``):
    when ``env``, ``MCP_SERVER_BASE_URL`` in the process environment overrides
    these DB rows at tool-call time.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        from uhc_execution_engine.mcp_client import active_config_source

        qs = McpServerConfig.objects.all()
        return Response({
            "runtime_config_source": active_config_source(),
            "servers": McpServerConfigSerializer(qs, many=True).data,
        })

    def post(self, request, *args, **kwargs):
        ser = McpServerConfigSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data, status=status.HTTP_201_CREATED)


class McpServerDetailView(APIView):
    """``GET`` / ``PUT`` / ``PATCH`` / ``DELETE`` one MCP server config."""

    permission_classes = [IsAuthenticated]

    def _get(self, pk):
        return McpServerConfig.objects.filter(pk=pk).first()

    def get(self, request, pk, *args, **kwargs):
        obj = self._get(pk)
        if not obj:
            return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(McpServerConfigSerializer(obj).data)

    def put(self, request, pk, *args, **kwargs):
        return self._update(request, pk, partial=False)

    def patch(self, request, pk, *args, **kwargs):
        return self._update(request, pk, partial=True)

    def _update(self, request, pk, *, partial):
        obj = self._get(pk)
        if not obj:
            return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
        ser = McpServerConfigSerializer(obj, data=request.data, partial=partial)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    def delete(self, request, pk, *args, **kwargs):
        obj = self._get(pk)
        if not obj:
            return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


_MCP_NOT_CONFIGURED = (
    "no active MCP server configured — set MCP_SERVER_BASE_URL (and MCP_SERVER_API_KEY) "
    "in the environment, or add/activate a row on the MCP Servers page"
)


class McpServerTestView(APIView):
    """``POST /api/agent-tools/mcp-servers/{pk}/test/`` — smart reachability check.

    Rather than pinging the bare ``base_url`` (which 404s on a server that only
    mounts ``/tools/*`` routes), this probes a **real tool route**:

    1. Path resolution — use ``body['path']`` if given, else auto-pick the
       ``metadata['mcp_path']`` of the first active tool that has one.
    2. Probe — call ``base_url + path`` with the server's ``http_method`` and a
       ``{claim_arg: sample}`` body (sample claim from ``body['claim_number']``
       when provided), using the stored auth header.

    Result semantics (``route_ok`` = the route exists, i.e. status is not 404):

    * ``ok`` (green)  — the probed tool route exists (2xx, or any non-404).
    * ``reachable``   — the host answered at all (TCP/TLS/HTTP succeeded).
    * network failure — ``reachable=False`` (red).

    When no tool path is available we fall back to a bare ``base_url`` ping so
    the user still gets a host-up/host-down signal. Read-only; mutates nothing.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        cfg_row = McpServerConfig.objects.filter(pk=pk).first()
        if not cfg_row:
            return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)

        body = request.data if isinstance(request.data, dict) else {}
        explicit_path = (body.get("path") or "").strip()
        sample_claim = str(body.get("claim_number") or body.get("claim") or "").strip()

        from uhc_execution_engine.mcp_client import check_mcp_health_with_config

        cfg = {
            "base_url": (cfg_row.base_url or "").rstrip("/"),
            "auth_header": cfg_row.auth_header or "x-api-key",
            "api_key": cfg_row.api_key or "",
            "http_method": (cfg_row.http_method or "POST").upper(),
            "claim_arg": cfg_row.claim_arg or "claim_number",
            "timeout": cfg_row.timeout_seconds or 30,
        }
        health = check_mcp_health_with_config(
            cfg,
            claim_id=sample_claim,
            explicit_path=explicit_path,
        )
        payload = {
            "ok": health.get("ok"),
            "reachable": health.get("reachable"),
            "status_code": health.get("status_code"),
            "latency_ms": health.get("latency_ms"),
            "url": health.get("url"),
            "probed_tool": health.get("probed_tool"),
        }
        if health.get("route_ok") is not None:
            payload["route_ok"] = health.get("route_ok")
        if sample_claim:
            payload["sample_claim"] = sample_claim
        if health.get("error"):
            payload["error"] = health.get("error")
        if not explicit_path and not health.get("probed_tool") and health.get("reachable"):
            payload["note"] = (
                "no tool has an mcp_path yet — set one on the Tool Calls "
                "page to verify a real route."
            )
        return Response(payload)


class ToolInvokeView(APIView):
    """``POST /api/agent-tools/{name}/invoke`` — execute / test a tool.

    Routing mirrors what the execution engine does at runtime:

    * If the tool row carries ``metadata['mcp_path']`` we route the call to the
      active :class:`McpServerConfig` (``base_url + path``). This is the only
      way to test a tool that was *added from the UI* and has no in-process
      Python implementation.
    * Otherwise (or when no active MCP server is configured but an in-process
      implementation exists) we fall back to the local LangGraph single-tool
      runtime.
    """

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

        mcp_path = (tool.metadata or {}).get("mcp_path") if isinstance(tool.metadata, dict) else None

        # ── MCP route ──────────────────────────────────────────────────────
        if mcp_path:
            try:
                from uhc_execution_engine.mcp_client import mcp_invoke
            except Exception as exc:  # pragma: no cover - engine not importable
                return Response(
                    {"ok": False, "tool": tool.name,
                     "error": f"MCP routing unavailable: {exc}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            routed = mcp_invoke(tool.name, args)
            if routed is None:
                return Response(
                    {"ok": False, "tool": tool.name,
                     "error": f"{_MCP_NOT_CONFIGURED}, or clear this tool's mcp_path "
                              "to run the in-process implementation."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response(
                {"ok": bool(routed.get("ok")), "tool": tool.name,
                 "result": routed.get("result"), "error": routed.get("error") or "",
                 "duration_ms": routed.get("duration_ms")},
                status=status.HTTP_200_OK,
            )

        # ── In-process route ───────────────────────────────────────────────
        # Lazy import to keep the URL conf import cheap and to avoid pulling
        # langgraph during Django startup.
        from .graphs.single_tool_graph import run_tool

        try:
            result = run_tool(tool.name, args)
        except LookupError:
            # A tool that was added from the UI but has neither an mcp_path nor a
            # registered in-process implementation cannot be executed.
            return Response(
                {"ok": False, "tool": tool.name,
                 "error": f"'{tool.name}' has no in-process implementation. Set an "
                          "mcp_path on this tool to route it to the active MCP server."},
                status=status.HTTP_400_BAD_REQUEST,
            )
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


def _execute_tool(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    """Run a tool (MCP route → in-process fallback) and return a result dict.

    Shared by the analyze endpoint so it can fetch a fresh payload when the
    caller did not pass one.
    """
    mcp_path = (tool.metadata or {}).get("mcp_path") if isinstance(tool.metadata, dict) else None
    if mcp_path:
        from uhc_execution_engine.mcp_client import mcp_invoke
        routed = mcp_invoke(tool.name, args)
        if routed is None:
            return {"ok": False, "result": None, "error": _MCP_NOT_CONFIGURED}
        return {"ok": bool(routed.get("ok")), "result": routed.get("result"),
                "error": routed.get("error") or ""}
    from .graphs.single_tool_graph import run_tool
    result = run_tool(tool.name, args)
    return {"ok": True, "result": result, "error": ""}


class ToolAnalyzeView(APIView):
    """``POST /api/agent-tools/{name}/analyze`` — understand a tool's response.

    Sends the tool's response payload to an LLM (Claude / OpenAI) which extracts
    a structured understanding (summary + per-field meaning) and caches it in
    the :class:`~agent_tools.models.McpToolContext` store keyed to the tool.

    Body may carry either:
      * ``result`` — an already-fetched response (avoids a second MCP call), or
      * ``args``   — invoke arguments; the tool is executed first.
    """

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
        result = body.get("result", None)

        # Fetch a payload if the caller didn't pass one.
        if result is None:
            call_args = body.get("args") or {}
            if not isinstance(call_args, dict):
                return Response(
                    {"ok": False, "error": "'args' must be a JSON object"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                outcome = _execute_tool(tool, call_args)
            except LookupError:
                return Response(
                    {"ok": False, "tool": tool.name,
                     "error": f"'{tool.name}' has no in-process implementation; "
                              "set an mcp_path to route it to the active MCP server."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("analyze: tool '%s' invoke failed", tool.name)
                return Response(
                    {"ok": False, "tool": tool.name, "error": str(exc)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            if not outcome.get("ok"):
                return Response(
                    {"ok": False, "tool": tool.name,
                     "error": outcome.get("error") or "tool call failed"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            result = outcome.get("result")

        from .context_analyzer import analyze_response

        try:
            analysis = analyze_response(tool.name, tool.description or "", result)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("analyze: understanding '%s' failed", tool.name)
            return Response(
                {"ok": False, "tool": tool.name, "error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        from django.utils import timezone

        server = McpServerConfig.objects.filter(is_active=True).order_by("-updated_at").first()
        mcp_path = (tool.metadata or {}).get("mcp_path", "") if isinstance(tool.metadata, dict) else ""
        ctx, _created = McpToolContext.objects.update_or_create(
            tool=tool,
            defaults={
                "server": server,
                "mcp_path": mcp_path or "",
                "summary": analysis["summary"],
                "fields": analysis["fields"],
                "sample_response": analysis["sample_response"],
                "record_count": analysis["record_count"],
                "truncated": analysis["truncated"],
                "llm_provider": analysis["llm_provider"],
                "llm_model": analysis["llm_model"],
                "analyzed_at": timezone.now(),
            },
        )
        return Response(
            {"ok": True, "tool": tool.name, "context": McpToolContextSerializer(ctx).data},
            status=status.HTTP_200_OK,
        )


class ToolContextView(APIView):
    """``GET /api/agent-tools/{name}/context`` — fetch the cached understanding.

    Returns ``{"ok": True, "context": null}`` when the tool has never been
    analyzed, so the UI can decide whether to show a "Understand response"
    affordance or the stored context.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, name: str, *args, **kwargs):
        try:
            tool = Tool.objects.get(name=name)
        except Tool.DoesNotExist:
            return Response(
                {"ok": False, "error": f"unknown tool '{name}'"},
                status=status.HTTP_404_NOT_FOUND,
            )
        ctx = McpToolContext.objects.filter(tool=tool).first()
        return Response(
            {"ok": True, "tool": tool.name,
             "context": McpToolContextSerializer(ctx).data if ctx else None},
            status=status.HTTP_200_OK,
        )
