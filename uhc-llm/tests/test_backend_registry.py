"""Tests for env-driven LLM backend selection and registry parsing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from uhc_llm.backend import get_llm_backend
from uhc_llm.paths import config_root
from uhc_llm.registry import (
    global_registry_model_name,
    load_agent_model_map,
    load_model_registry,
    resolve_registry_model_name,
)


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    load_model_registry.cache_clear()
    load_agent_model_map.cache_clear()
    yield
    load_model_registry.cache_clear()
    load_agent_model_map.cache_clear()


def test_backend_defaults_to_api_key(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.delenv("MODEL_REGISTRY", raising=False)
    assert get_llm_backend() == "api_key"


def test_backend_explicit_registry(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "registry")
    assert get_llm_backend() == "registry"


def test_backend_auto_registry_when_model_registry_set(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.setenv("MODEL_REGISTRY", "uhg-gateway")
    assert get_llm_backend() == "registry"


def test_app_wide_llm_model_without_agent_map(monkeypatch):
    """LLM_MODEL routes every agent to the same registry key."""
    monkeypatch.delenv("AGENT_MODEL_MAP", raising=False)
    monkeypatch.delenv("REGISTRY_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("MODEL_REGISTRY", "uhg-gateway")
    monkeypatch.setenv("LLM_MODEL", "opus")

    assert global_registry_model_name() == "opus"
    assert resolve_registry_model_name("NpiMatch") == "opus"
    assert resolve_registry_model_name("date_condition_extractor") == "opus"
    assert resolve_registry_model_name("any_random_agent") == "opus"


def test_llm_model_overrides_agent_map(monkeypatch):
    monkeypatch.setenv("MODEL_REGISTRY", "uhg-gateway")
    monkeypatch.setenv("AGENT_MODEL_MAP", "execution")
    monkeypatch.setenv("LLM_MODEL", "opus")

    # execution map would give NpiMatch → llama, but LLM_MODEL wins app-wide
    assert resolve_registry_model_name("NpiMatch") == "opus"


def test_load_bundled_registry_profile_with_agent_map(monkeypatch):
    monkeypatch.delenv("UHC_LLM_CONFIG_DIR", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("MODEL_REGISTRY", "uhg-gateway")
    monkeypatch.setenv("AGENT_MODEL_MAP", "execution")

    specs = load_model_registry()
    assert specs["gpt-5-mini"].kind == "azure_openai"
    assert specs["opus"].kind == "bedrock_claude"
    assert resolve_registry_model_name("NpiMatch") == "llama"
    assert resolve_registry_model_name("unknown_agent") == "gpt-5-mini"


def test_load_ingestion_agent_map(monkeypatch):
    monkeypatch.setenv("MODEL_REGISTRY", "uhg-gateway")
    monkeypatch.setenv("AGENT_MODEL_MAP", "ingestion")

    assert resolve_registry_model_name("rule_semantic_enricher") == "opus"
    assert resolve_registry_model_name("date_condition_extractor") == "gpt-5-mini"


def test_inline_json_still_supported(monkeypatch):
    registry = {
        "gpt-5-mini": {
            "kind": "azure_openai",
            "endpoint": "https://api.uhg.com/gateway",
            "deployment": "gpt-5-mini_2025-08-07",
            "api_version": "2025-01-01-preview",
        },
        "opus": {
            "kind": "bedrock_claude",
            "endpoint": "https://api.uhg.com/gateway",
            "deployment": "us.anthropic.claude-opus-4-6-v1",
        },
    }
    agent_map = {
        "rule_evaluator": "opus",
        "__default__": "gpt-5-mini",
    }
    monkeypatch.setenv("MODEL_REGISTRY", json.dumps(registry))
    monkeypatch.setenv("AGENT_MODEL_MAP", json.dumps(agent_map))

    specs = load_model_registry()
    assert specs["gpt-5-mini"].api_version == "2025-01-01-preview"
    assert resolve_registry_model_name("rule_evaluator") == "opus"


def test_custom_config_dir(tmp_path, monkeypatch):
    reg_dir = tmp_path / "registries"
    map_dir = tmp_path / "agent_maps"
    reg_dir.mkdir()
    map_dir.mkdir()
    (reg_dir / "custom.json").write_text(
        json.dumps({
            "mini": {
                "kind": "openai_compat",
                "endpoint": "https://example",
                "deployment": "mini",
            }
        }),
        encoding="utf-8",
    )
    (map_dir / "custom.json").write_text(
        json.dumps({"my_agent": "mini", "__default__": "mini"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("UHC_LLM_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_REGISTRY", "custom")
    monkeypatch.setenv("AGENT_MODEL_MAP", "custom")

    assert load_model_registry()["mini"].deployment == "mini"
    assert resolve_registry_model_name("my_agent") == "mini"


def test_bundled_config_root_exists():
    root = config_root()
    assert (root / "registries" / "uhg-gateway.json").is_file()
    assert (root / "agent_maps" / "execution.json").is_file()
