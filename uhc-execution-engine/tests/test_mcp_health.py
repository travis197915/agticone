"""Tests for once-per-claim MCP health gating before tool calls."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import requests

from uhc_execution_engine.mcp_client import (
    check_mcp_health_with_config,
    format_mcp_health_error,
    mcp_health_check_enabled,
    should_run_mcp_health_check,
)


_CFG = {
    "base_url": "https://mcp.example.com",
    "auth_header": "x-api-key",
    "api_key": "secret",
    "http_method": "POST",
    "claim_arg": "claim_number",
    "timeout": 5,
}


def test_mcp_health_check_enabled_default(monkeypatch):
    monkeypatch.delenv("MCP_HEALTH_CHECK_ENABLED", raising=False)
    assert mcp_health_check_enabled() is True


def test_mcp_health_check_disabled(monkeypatch):
    monkeypatch.setenv("MCP_HEALTH_CHECK_ENABLED", "false")
    assert mcp_health_check_enabled() is False


def test_should_run_false_when_disabled(monkeypatch):
    monkeypatch.setenv("MCP_HEALTH_CHECK_ENABLED", "off")
    monkeypatch.setattr(
        "uhc_execution_engine.mcp_client._active_config",
        lambda: _CFG,
    )
    assert should_run_mcp_health_check(fetch_tool="facets_get_summary") is False


def test_should_run_false_without_mcp_config(monkeypatch):
    monkeypatch.delenv("MCP_HEALTH_CHECK_ENABLED", raising=False)
    monkeypatch.setattr(
        "uhc_execution_engine.mcp_client._active_config",
        lambda: None,
    )
    assert should_run_mcp_health_check(fetch_tool="facets_get_summary") is False


def test_should_run_true_when_fetch_tool_has_mcp_path(monkeypatch):
    monkeypatch.delenv("MCP_HEALTH_CHECK_ENABLED", raising=False)
    monkeypatch.setattr(
        "uhc_execution_engine.mcp_client._active_config",
        lambda: _CFG,
    )
    monkeypatch.setattr(
        "uhc_execution_engine.mcp_client._tool_path",
        lambda name: "/tools/facets_get_summary" if name else "",
    )
    assert should_run_mcp_health_check(fetch_tool="facets_get_summary") is True


def test_check_mcp_health_route_ok(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="uhc_execution_engine.mcp_client")
    caplog.set_level(logging.INFO, logger="uhc_execution_engine.tool_telemetry")
    resp = MagicMock()
    resp.status_code = 200
    monkeypatch.setattr(
        "uhc_execution_engine.mcp_client.requests.request",
        lambda *a, **k: resp,
    )
    out = check_mcp_health_with_config(
        _CFG,
        tool_name="facets_get_summary",
        claim_id="CLM001",
        explicit_path="/tools/facets_get_summary",
    )
    assert out["ok"] is True
    assert out["reachable"] is True
    assert out["status_code"] == 200
    assert "facets_get_summary" in out["url"]
    assert any("mcp_health start" in r.message for r in caplog.records)
    assert any("mcp_health ok" in r.message for r in caplog.records)
    assert any("tool_call [HEALTH]" in r.message for r in caplog.records)


def test_check_mcp_health_connection_error(monkeypatch):
    def _boom(*_a, **_k):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr("uhc_execution_engine.mcp_client.requests.request", _boom)
    out = check_mcp_health_with_config(
        _CFG,
        explicit_path="/tools/facets_get_summary",
        claim_id="CLM001",
    )
    assert out["ok"] is False
    assert out["reachable"] is False
    assert "refused" in out["error"]


def test_check_mcp_health_404_not_ok(monkeypatch):
    resp = MagicMock()
    resp.status_code = 404
    monkeypatch.setattr(
        "uhc_execution_engine.mcp_client.requests.request",
        lambda *a, **k: resp,
    )
    out = check_mcp_health_with_config(
        _CFG,
        explicit_path="/tools/missing",
        claim_id="CLM001",
    )
    assert out["ok"] is False
    assert out["status_code"] == 404


def test_format_mcp_health_error():
    msg = format_mcp_health_error({
        "error": "Connection refused",
        "url": "https://mcp.example.com/tools/x",
        "status_code": None,
    })
    assert msg.startswith("Claims tool service unavailable:")
    assert "Connection refused" in msg
    assert "https://mcp.example.com/tools/x" in msg
    assert "status=None" in msg


@patch("uhc_execution_engine.batch.timezone")
@patch("uhc_execution_engine.batch.fetch_claim")
@patch("uhc_execution_engine.batch.should_run_mcp_health_check", return_value=True)
@patch("uhc_execution_engine.batch.check_mcp_health")
def test_batch_run_one_fails_before_fetch(mock_health, _mock_should, mock_fetch, mock_tz):
    from uhc_execution_engine.batch import BatchRunner

    mock_tz.now.return_value = "2026-01-01T00:00:00Z"
    mock_health.return_value = {
        "ok": False,
        "error": "Connection refused",
        "url": "https://mcp.example.com/tools/facets_get_summary",
        "status_code": None,
        "probed_tool": "facets_get_summary",
    }
    mock_models = MagicMock()
    mock_models.RuleExecutionRun.objects.create.return_value = MagicMock()
    with patch.dict(sys.modules, {"execution_app.models": mock_models}):
        out = BatchRunner()._run_one(
            workflow_id="wf-1",
            claim_id="CLM001",
            batch_id="batch-1",
            use_parser=False,
            fetch_tool="facets_get_summary",
        )
    mock_fetch.assert_not_called()
    assert out["status"] == "FAILED"
    assert "Claims tool service unavailable" in out["error_message"]
    assert out["tool_invocations"] == []
