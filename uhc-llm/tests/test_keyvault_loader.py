"""Tests for Azure Key Vault loader (mocked — no live vault)."""
from __future__ import annotations

import json
import os

import pytest

try:
    from azure.core.exceptions import ResourceNotFoundError
except ImportError:
    class ResourceNotFoundError(Exception):  # type: ignore[no-redef]
        """Stub when azure-core is not installed in the test env."""

from uhc_llm.keyvault_loader import (
    LLM_KV_SECRET_NAMES,
    _llm_secret_names_from_env,
    bootstrap_llm_secrets,
    kv_name_to_env_key,
    keyvault_configured,
    load_secrets_into_env,
)
from uhc_llm.oauth import clear_oauth_token_cache
from uhc_llm.registry import load_model_registry, refresh_model_registry


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    for name in (
        "AZURE_KEY_VAULT_URL",
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_SECRET",
        "AUTH_URL",
        "CLIENT_ID",
        "CLIENT_SECRET",
        "SCOPE",
        "MODEL_REGISTRY_JSON",
        "LLM_BACKEND",
        "KEYVAULT_LOAD_ALL_SECRETS",
        "KEYVAULT_LLM_SECRET_NAMES",
    ):
        monkeypatch.delenv(name, raising=False)
    clear_oauth_token_cache()
    load_model_registry.cache_clear()
    import uhc_llm.keyvault_loader as kv

    kv._loaded = False
    yield
    kv._loaded = False
    clear_oauth_token_cache()
    load_model_registry.cache_clear()


def test_kv_name_to_env_key():
    assert kv_name_to_env_key("CLIENT-ID") == "CLIENT_ID"
    assert kv_name_to_env_key("MODEL-REGISTRY-JSON") == "MODEL_REGISTRY_JSON"


def test_keyvault_configured_requires_all_four(monkeypatch):
    assert not keyvault_configured()
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://vault.example.net/")
    monkeypatch.setenv("AZURE_CLIENT_ID", "cid")
    monkeypatch.setenv("AZURE_TENANT_ID", "tid")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret")
    assert keyvault_configured()


def test_llm_secret_names_from_env_accepts_env_style_override(monkeypatch):
    monkeypatch.setenv("KEYVAULT_LLM_SECRET_NAMES", "AUTH_URL,CLIENT_ID")
    names = _llm_secret_names_from_env()
    assert "AUTH_URL" in names
    assert "AUTH-URL" in names
    assert "CLIENT_ID" in names
    assert "CLIENT-ID" in names


def test_load_secrets_into_env_focused_scope(monkeypatch):
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://vault.example.net/")
    monkeypatch.setenv("AZURE_CLIENT_ID", "cid")
    monkeypatch.setenv("AZURE_TENANT_ID", "tid")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("CLIENT_ID", "already-set")

    requested: list[str] = []

    class _FakeSecret:
        def __init__(self, value: str):
            self.value = value

    class _FakeClient:
        def get_secret(self, name: str):
            requested.append(name)
            if name == "AUTH-URL":
                return _FakeSecret("value-for-AUTH-URL")
            raise ResourceNotFoundError("not found")

        def list_properties_of_secrets(self):
            raise AssertionError("focused scope should not list all secrets")

    monkeypatch.setattr(
        "uhc_llm.keyvault_loader._build_client",
        lambda: _FakeClient(),
    )

    injected = load_secrets_into_env(scope="llm")
    assert "AUTH-URL" in requested
    assert os.environ["CLIENT_ID"] == "already-set"
    assert os.environ["AUTH_URL"] == "value-for-AUTH-URL"
    assert injected["AUTH_URL"] == "value-for-AUTH-URL"


def test_load_secrets_into_env_all_scope(monkeypatch):
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://vault.example.net/")
    monkeypatch.setenv("AZURE_CLIENT_ID", "cid")
    monkeypatch.setenv("AZURE_TENANT_ID", "tid")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret")

    class _Prop:
        def __init__(self, name: str):
            self.name = name
            self.enabled = True

    class _FakeSecret:
        def __init__(self, value: str):
            self.value = value

    class _FakeClient:
        def list_properties_of_secrets(self):
            return [_Prop("AUTH-URL")]

        def get_secret(self, name: str):
            return _FakeSecret(f"value-for-{name}")

    monkeypatch.setattr(
        "uhc_llm.keyvault_loader._build_client",
        lambda: _FakeClient(),
    )

    injected = load_secrets_into_env(scope="all")
    assert injected["AUTH_URL"] == "value-for-AUTH-URL"


def test_bootstrap_refreshes_registry_after_kv(monkeypatch):
    registry_json = {
        "mini": {
            "kind": "openai_compat",
            "endpoint": "https://example",
            "deployment": "mini",
        }
    }

    class _FakeSecret:
        def __init__(self, value: str):
            self.value = value

    class _FakeClient:
        def get_secret(self, name: str):
            if name == "MODEL-REGISTRY-JSON":
                return _FakeSecret(json.dumps(registry_json))
            raise ResourceNotFoundError("not found")

    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://vault.example.net/")
    monkeypatch.setenv("AZURE_CLIENT_ID", "cid")
    monkeypatch.setenv("AZURE_TENANT_ID", "tid")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(
        "uhc_llm.keyvault_loader._build_client",
        lambda: _FakeClient(),
    )

    assert load_model_registry() == {}

    bootstrap_llm_secrets()
    specs = load_model_registry()
    assert "mini" in specs
    assert specs["mini"].deployment == "mini"


def test_bootstrap_all_scope_when_flag_set(monkeypatch):
    monkeypatch.setenv("KEYVAULT_LOAD_ALL_SECRETS", "true")
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://vault.example.net/")
    monkeypatch.setenv("AZURE_CLIENT_ID", "cid")
    monkeypatch.setenv("AZURE_TENANT_ID", "tid")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret")

    listed = {"called": False}

    class _Prop:
        name = "SCOPE"
        enabled = True

    class _FakeSecret:
        value = "api://default"

    class _FakeClient:
        def list_properties_of_secrets(self):
            listed["called"] = True
            return [_Prop()]

        def get_secret(self, name: str):
            return _FakeSecret()

    monkeypatch.setattr(
        "uhc_llm.keyvault_loader._build_client",
        lambda: _FakeClient(),
    )

    bootstrap_llm_secrets()
    assert listed["called"]
    assert os.environ["SCOPE"] == "api://default"


def test_refresh_model_registry_clears_cache(monkeypatch):
    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps({
        "a": {"kind": "openai_compat", "endpoint": "https://x", "deployment": "a"},
    }))
    first = load_model_registry()
    assert "a" in first

    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps({
        "b": {"kind": "openai_compat", "endpoint": "https://y", "deployment": "b"},
    }))
    still_a = load_model_registry()
    assert "a" in still_a

    refreshed = refresh_model_registry()
    assert "b" in refreshed
    assert "a" not in refreshed


def test_default_llm_secret_list_includes_oauth_and_registry():
    assert "AUTH-URL" in LLM_KV_SECRET_NAMES
    assert "MODEL-REGISTRY-JSON" in LLM_KV_SECRET_NAMES
