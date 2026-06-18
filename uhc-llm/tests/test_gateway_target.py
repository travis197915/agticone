"""Tests for gateway request target descriptions."""
from __future__ import annotations

from uhc_llm.gateway import (
    _anthropic_base_url,
    _anthropic_model_candidates,
    describe_registry_target,
)
from uhc_llm.registry import ModelSpec


def test_describe_azure_openai_target():
    spec = ModelSpec(
        name="gpt-5-mini",
        kind="azure_openai",
        endpoint="https://api.uhg.com/gateway-reasoning/1.0",
        deployment="gpt-5-mini_2025-08-07",
        api_version="2025-01-01-preview",
    )
    target = describe_registry_target(spec, json_mode=True)
    assert "POST https://api.uhg.com/gateway-reasoning/1.0/openai/deployments/gpt-5-mini_2025-08-07/chat/completions" in target
    assert "api-version=2025-01-01-preview" in target
    assert "registry='gpt-5-mini'" in target


def test_describe_openai_compat_target():
    spec = ModelSpec(
        name="llama",
        kind="openai_compat",
        endpoint="https://api.uhg.com/ai-gateway/1.0",
        deployment="llama-3-3_70b-instruct",
    )
    target = describe_registry_target(spec)
    assert "POST https://api.uhg.com/ai-gateway/1.0/v1/chat/completions" in target
    assert "deployment='llama-3-3_70b-instruct'" in target


def test_describe_bedrock_claude_target():
    spec = ModelSpec(
        name="opus",
        kind="bedrock_claude",
        endpoint="https://api.uhg.com/ai-gateway/1.0",
        deployment="us.anthropic.claude-opus-4-6-v1",
    )
    target = describe_registry_target(spec)
    assert "POST https://api.uhg.com/ai-gateway/1.0/anthropic/v1/messages" in target
    assert "deployment='us.anthropic.claude-opus-4-6-v1'" in target


def test_anthropic_base_url_appends_anthropic_subpath():
    assert _anthropic_base_url("https://api.uhg.com/ai-gateway/1.0/") == (
        "https://api.uhg.com/ai-gateway/1.0/anthropic"
    )
    assert _anthropic_base_url("https://api.uhg.com/ai-gateway/1.0/anthropic") == (
        "https://api.uhg.com/ai-gateway/1.0/anthropic"
    )


def test_anthropic_model_candidates_strips_bedrock_prefix_and_v1():
    names = _anthropic_model_candidates("us.anthropic.claude-opus-4-6-v1")
    assert names == [
        "us.anthropic.claude-opus-4-6-v1",
        "claude-opus-4-6-v1",
        "claude-opus-4-6",
    ]
