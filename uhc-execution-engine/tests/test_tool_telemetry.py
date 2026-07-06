"""Tests for structured tool-call logging during claim processing."""
from __future__ import annotations

import logging

import pytest
import requests

from uhc_execution_engine.llm import execution_run_context
from uhc_execution_engine.mcp_client import mcp_invoke, parallel_tool_invoke_timeout_seconds
from uhc_execution_engine.tool_runner import invoke_tool
from uhc_execution_engine.tool_telemetry import classify_tool_error, log_tool_call


def test_log_tool_call_includes_run_id(caplog):
    caplog.set_level(logging.INFO, logger="uhc_execution_engine.tool_telemetry")

    with execution_run_context("run-abc-123"):
        log_tool_call(
            tool_name="facets_get_summary",
            phase="FETCH",
            ok=True,
            duration_ms=42,
            route="mcp",
            claim_id="CLM001",
            args={"claim_number": "CLM001"},
            timeout_s=30,
        )

    assert any(
        "tool_call [FETCH] facets_get_summary" in r.message
        and "run=run-abc-123" in r.message
        and "claim=CLM001" in r.message
        and "route=mcp" in r.message
        and "timeout_s=30" in r.message
        for r in caplog.records
    )


def test_log_tool_call_timeout_uses_warning(caplog):
    caplog.set_level(logging.INFO, logger="uhc_execution_engine.tool_telemetry")

    log_tool_call(
        tool_name="facets_get_summary",
        phase="FETCH",
        ok=False,
        duration_ms=30001,
        route="mcp",
        claim_id="CLM001",
        error="Read timed out",
        timeout_s=30,
        error_kind="timeout",
        attempts=3,
    )

    assert any(r.levelno == logging.WARNING and "error_kind=timeout" in r.message
               for r in caplog.records)


def test_classify_tool_error_timeout():
    assert classify_tool_error(requests.exceptions.Timeout()) == "timeout"
    assert classify_tool_error(None, error_text="parallel tool invoke exceeded 100s") == "timeout"


def test_parallel_tool_invoke_timeout_default(monkeypatch):
    monkeypatch.delenv("RULE_ENGINE_TOOL_INVOKE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("MCP_SERVER_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr(
        "uhc_execution_engine.mcp_client._active_config",
        lambda: None,
    )
    assert parallel_tool_invoke_timeout_seconds() == 100.0


def test_invoke_tool_logs_missing_registry_tool(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="uhc_execution_engine.tool_telemetry")
    monkeypatch.setattr(
        "uhc_execution_engine.mcp_client.mcp_invoke",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "agent_tools.registry.get_tool",
        lambda _name: None,
    )

    with execution_run_context("run-missing"):
        out = invoke_tool(
            "does_not_exist",
            {"claim_number": "X"},
            phase="EVALUATE",
            claim_id="X",
        )

    assert out["ok"] is False
    assert any("route=missing" in r.message for r in caplog.records)


def test_mcp_invoke_logs_timeout(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="uhc_execution_engine.mcp_client")

    monkeypatch.setattr(
        "uhc_execution_engine.mcp_client._active_config",
        lambda: {
            "base_url": "https://mcp.example",
            "auth_header": "x-api-key",
            "api_key": "k",
            "http_method": "POST",
            "claim_arg": "claim_number",
            "timeout": 5,
        },
    )
    monkeypatch.setattr(
        "uhc_execution_engine.mcp_client._tool_path",
        lambda _name: "/tools/facets_get_summary",
    )

    def _timeout(*_a, **_k):
        raise requests.exceptions.Timeout("read timed out")

    monkeypatch.setattr("uhc_execution_engine.mcp_client.requests.request", _timeout)

    out = mcp_invoke("facets_get_summary", {"claim_number": "CLM1"})
    assert out is not None
    assert out["ok"] is False
    assert out["error_kind"] == "timeout"
    assert any("mcp_invoke TIMEOUT" in r.message for r in caplog.records)
