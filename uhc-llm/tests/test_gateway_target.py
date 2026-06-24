"""Tests for transport route descriptors."""
from __future__ import annotations

from uhc_llm.registry import ModelSpec
from uhc_llm.routes import (
    anthropic_direct_messages_url,
    anthropic_gateway_messages_url,
    anthropic_gateway_sdk_base,
    bedrock_invoke_url,
    describe_api_key_target,
    describe_registry_target,
)


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
    assert "POST https://api.uhg.com/ai-gateway/1.0/model/us.anthropic.claude-opus-4-6-v1/invoke" in target
    assert "deployment='us.anthropic.claude-opus-4-6-v1'" in target


def test_anthropic_direct_vs_gateway_paths():
    assert anthropic_direct_messages_url() == "https://api.anthropic.com/v1/messages"
    assert anthropic_gateway_sdk_base("https://api.uhg.com/ai-gateway/1.0/") == (
        "https://api.uhg.com/ai-gateway/1.0/anthropic"
    )
    assert anthropic_gateway_messages_url("https://api.uhg.com/ai-gateway/1.0") == (
        "https://api.uhg.com/ai-gateway/1.0/anthropic/v1/messages"
    )
    assert bedrock_invoke_url(
        "https://api.uhg.com/ai-gateway/1.0",
        "us.anthropic.claude-opus-4-6-v1",
    ) == (
        "https://api.uhg.com/ai-gateway/1.0/model/"
        "us.anthropic.claude-opus-4-6-v1/invoke"
    )


def test_describe_api_key_target():
    assert "/v1/messages" in describe_api_key_target(provider="anthropic", model="claude")
    assert "api_key" in describe_api_key_target(provider="openai")
