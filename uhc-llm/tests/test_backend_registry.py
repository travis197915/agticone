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
    registry_profile_name,
    resolve_pdf_registry_model_name,
    resolve_registry_model_name,
)


SAMPLE_REGISTRY = {
    "gpt-5-mini": {
        "kind": "azure_openai",
        "endpoint": "https://gateway.example/reasoning/1.0",
        "deployment": "gpt-5-mini_2025-08-07",
        "api_version": "2025-01-01-preview",
    },
    "llama": {
        "kind": "openai_compat",
        "endpoint": "https://gateway.example/ai-gateway/1.0",
        "deployment": "llama-3-3_70b-instruct",
    },
    "opus": {
        "kind": "bedrock_claude",
        "endpoint": "https://gateway.example/ai-gateway/1.0",
        "deployment": "us.anthropic.claude-opus-4-6-v1",
    },
}


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    load_model_registry.cache_clear()
    load_agent_model_map.cache_clear()
    yield
    load_model_registry.cache_clear()
    load_agent_model_map.cache_clear()


@pytest.fixture(autouse=True)
def _clear_registry_env(monkeypatch):
    for name in (
        "MODEL_REGISTRY",
        "MODEL_REGISTRY_JSON",
        "MODEL_REGISTRY_FILE",
        "AGENT_MODEL_MAP",
        "LLM_MODEL",
        "LLM_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)


def test_backend_defaults_to_api_key():
    assert get_llm_backend() == "api_key"


def test_backend_explicit_registry(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "registry")
    assert get_llm_backend() == "registry"


def test_backend_auto_registry_from_model_registry_json(monkeypatch):
    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps(SAMPLE_REGISTRY))
    assert get_llm_backend() == "registry"


def test_model_registry_json_takes_priority_over_profile(monkeypatch, tmp_path):
    reg_dir = tmp_path / "registries"
    reg_dir.mkdir()
    (reg_dir / "uhg-gateway.json").write_text(
        json.dumps({"wrong": {
            "kind": "openai_compat",
            "endpoint": "https://wrong",
            "deployment": "wrong",
        }}),
        encoding="utf-8",
    )
    monkeypatch.setenv("UHC_LLM_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_REGISTRY", "uhg-gateway")
    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps(SAMPLE_REGISTRY))

    specs = load_model_registry()
    assert "gpt-5-mini" in specs
    assert "wrong" not in specs
    assert registry_profile_name() == "MODEL_REGISTRY_JSON"


def test_model_registry_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "model-registry.local.json"
    secret_file.write_text(json.dumps(SAMPLE_REGISTRY), encoding="utf-8")
    monkeypatch.setenv("MODEL_REGISTRY_FILE", str(secret_file))

    specs = load_model_registry()
    assert specs["opus"].kind == "bedrock_claude"
    assert registry_profile_name() == f"MODEL_REGISTRY_FILE:{secret_file}"


def test_app_wide_llm_model_without_agent_map(monkeypatch):
    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps(SAMPLE_REGISTRY))
    monkeypatch.setenv("LLM_MODEL", "opus")

    assert global_registry_model_name() == "opus"
    assert resolve_registry_model_name("NpiMatch") == "opus"
    assert resolve_registry_model_name("any_random_agent") == "opus"


def test_llm_model_overrides_agent_map(monkeypatch):
    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps(SAMPLE_REGISTRY))
    monkeypatch.setenv("AGENT_MODEL_MAP", "execution")
    monkeypatch.setenv("LLM_MODEL", "opus")

    assert resolve_registry_model_name("NpiMatch") == "opus"


def test_invalid_llm_model_falls_back_to_agent_map(monkeypatch):
    """Job rows store provider model names; ignore them for registry routing."""
    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps(SAMPLE_REGISTRY))
    monkeypatch.setenv("AGENT_MODEL_MAP", "ingestion")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-5-20250929")

    assert resolve_registry_model_name("rule_semantic_enricher") == "opus"
    assert resolve_registry_model_name("date_condition_extractor") == "gpt-5-mini"


def test_agent_map_skips_keys_not_in_registry(monkeypatch):
    """Stale agent-map values must not win over valid registry keys."""
    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps(SAMPLE_REGISTRY))
    monkeypatch.setenv(
        "AGENT_MODEL_MAP",
        json.dumps({"my_agent": "removed-model", "__default__": "gpt-5-mini"}),
    )

    assert resolve_registry_model_name("my_agent") == "gpt-5-mini"


def test_gpt5_mini_fallback_when_no_agent_map_match(monkeypatch):
    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps(SAMPLE_REGISTRY))
    monkeypatch.setenv("AGENT_MODEL_MAP", json.dumps({"__default__": "also-missing"}))

    assert resolve_registry_model_name("unknown_agent") == "gpt-5-mini"


def test_apply_job_llm_env_skips_provider_model_in_registry_mode(monkeypatch):
    from uhc_llm.backend import apply_job_llm_env

    monkeypatch.setenv("LLM_BACKEND", "registry")
    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps(SAMPLE_REGISTRY))
    monkeypatch.setenv("LLM_MODEL", "opus")
    monkeypatch.setenv("AGENT_MODEL_MAP", "ingestion")

    apply_job_llm_env(
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-5-20250929",
    )
    assert global_registry_model_name() == "opus"

    apply_job_llm_env(llm_provider="anthropic", llm_model="gpt-5-mini")
    assert global_registry_model_name() == "gpt-5-mini"


def test_load_bundled_registry_profile_with_agent_map(monkeypatch):
    monkeypatch.delenv("UHC_LLM_CONFIG_DIR", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps(SAMPLE_REGISTRY))
    monkeypatch.setenv("AGENT_MODEL_MAP", "execution")

    specs = load_model_registry()
    assert specs["gpt-5-mini"].kind == "azure_openai"
    assert resolve_registry_model_name("NpiMatch") == "llama"
    assert resolve_registry_model_name("unknown_agent") == "gpt-5-mini"


def test_load_ingestion_agent_map(monkeypatch):
    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps(SAMPLE_REGISTRY))
    monkeypatch.setenv("AGENT_MODEL_MAP", "ingestion")

    assert resolve_registry_model_name("rule_semantic_enricher") == "opus"
    assert resolve_registry_model_name("date_condition_extractor") == "gpt-5-mini"
    assert resolve_pdf_registry_model_name("pdf_page_reader") == "opus"


def test_pdf_model_ignores_non_bedrock_llm_model(monkeypatch):
    from uhc_llm.registry import resolve_pdf_registry_model_name

    monkeypatch.setenv("MODEL_REGISTRY_JSON", json.dumps(SAMPLE_REGISTRY))
    monkeypatch.setenv("AGENT_MODEL_MAP", "ingestion")
    monkeypatch.setenv("LLM_MODEL", "gpt-5-mini")

    assert resolve_registry_model_name("pdf_page_reader") == "gpt-5-mini"
    assert resolve_pdf_registry_model_name("pdf_page_reader") == "opus"


def test_inline_json_still_supported(monkeypatch):
    agent_map = {
        "rule_evaluator": "opus",
        "__default__": "gpt-5-mini",
    }
    monkeypatch.setenv("MODEL_REGISTRY", json.dumps(SAMPLE_REGISTRY))
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


def test_bundled_example_and_agent_maps_exist():
    root = config_root()
    assert (root / "registries" / "uhg-gateway.example.json").is_file()
    assert (root / "agent_maps" / "execution.json").is_file()
    assert not (root / "registries" / "uhg-gateway.json").exists()
