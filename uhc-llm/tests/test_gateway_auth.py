"""Tests for gateway API key resolution."""
from __future__ import annotations

import pytest

from uhc_llm.gateway import (
    GATEWAY_KEY_ENV_VARS,
    _gateway_api_key,
    gateway_api_key_configured,
    gateway_api_key_source,
)


@pytest.fixture(autouse=True)
def _clear_key_env(monkeypatch):
    for name in GATEWAY_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_gateway_key_prefers_ai_gateway_api_key(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    assert gateway_api_key_source() == "AI_GATEWAY_API_KEY"
    assert _gateway_api_key() == "gw-key"


def test_gateway_key_falls_back_to_openai_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    assert gateway_api_key_source() == "OPENAI_API_KEY"
    assert gateway_api_key_configured()
    assert _gateway_api_key() == "openai-key"


def test_gateway_key_missing_raises(monkeypatch):
    from uhc_llm.oauth import oauth_configured

    assert not gateway_api_key_configured()
    assert not oauth_configured()
    with pytest.raises(RuntimeError, match="AUTH_URL|AI_GATEWAY_API_KEY"):
        _gateway_api_key()


def test_gateway_headers_include_apim_subscription_key(monkeypatch):
    from uhc_llm.gateway import _gateway_headers

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "sub-key")
    monkeypatch.setenv("PROJECT_ID", "proj-123")
    headers = _gateway_headers()
    assert headers["Ocp-Apim-Subscription-Key"] == "sub-key"
    assert headers["project-id"] == "proj-123"
