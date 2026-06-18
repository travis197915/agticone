"""Tests for OAuth client-credentials gateway auth."""
from __future__ import annotations

import pytest

from uhc_llm.gateway import (
    _gateway_headers,
    gateway_auth_configured,
    gateway_auth_source,
)
from uhc_llm.oauth import (
    OAUTH_ENV_VARS,
    clear_oauth_token_cache,
    fetch_oauth_token,
    oauth_configured,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for name in (
        *OAUTH_ENV_VARS,
        "SCOPE",
        "AI_GATEWAY_API_KEY",
        "APIM_SUBSCRIPTION_KEY",
        "OPENAI_API_KEY",
        "PROJECT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    clear_oauth_token_cache()
    yield
    clear_oauth_token_cache()


def test_oauth_configured_requires_all_vars(monkeypatch):
    assert not oauth_configured()
    monkeypatch.setenv("AUTH_URL", "https://login.example/token")
    monkeypatch.setenv("CLIENT_ID", "cid")
    monkeypatch.setenv("CLIENT_SECRET", "secret")
    assert not oauth_configured()
    monkeypatch.setenv("SCOPE", "api://default")
    assert oauth_configured()


def test_gateway_auth_prefers_oauth(monkeypatch):
    monkeypatch.setenv("AUTH_URL", "https://login.example/token")
    monkeypatch.setenv("CLIENT_ID", "cid")
    monkeypatch.setenv("CLIENT_SECRET", "secret")
    monkeypatch.setenv("SCOPE", "api://default")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "sub-key")

    assert gateway_auth_configured()
    assert gateway_auth_source() == "oauth:client_credentials"


def test_oauth_token_fetch_and_cache(monkeypatch):
    monkeypatch.setenv("AUTH_URL", "https://login.example/token")
    monkeypatch.setenv("CLIENT_ID", "cid")
    monkeypatch.setenv("CLIENT_SECRET", "secret")
    monkeypatch.setenv("SCOPE", "api://default")

    calls: list[dict] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"access_token": "tok-123", "expires_in": 3600}

    def _fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Resp()

    monkeypatch.setattr("uhc_llm.oauth.httpx.post", _fake_post)

    assert fetch_oauth_token() == "tok-123"
    assert fetch_oauth_token() == "tok-123"
    assert len(calls) == 1
    assert calls[0]["data"]["grant_type"] == "client_credentials"


def test_oauth_headers_use_bearer(monkeypatch):
    monkeypatch.setenv("AUTH_URL", "https://login.example/token")
    monkeypatch.setenv("CLIENT_ID", "cid")
    monkeypatch.setenv("CLIENT_SECRET", "secret")
    monkeypatch.setenv("SCOPE", "api://default")
    monkeypatch.setenv("PROJECT_ID", "proj-1")

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"access_token": "tok-abc", "expires_in": 3600}

    monkeypatch.setattr("uhc_llm.oauth.httpx.post", lambda *a, **k: _Resp())

    headers = _gateway_headers()
    assert headers["Authorization"] == "Bearer tok-abc"
    assert headers["project-id"] == "proj-1"
    assert "Ocp-Apim-Subscription-Key" not in headers
