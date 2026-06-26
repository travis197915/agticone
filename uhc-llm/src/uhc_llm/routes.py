"""Transport route descriptors for registry gateway vs direct API-key calls.

Direct Anthropic API-key clients (LangChain / Anthropic SDK) target
``https://api.anthropic.com/v1/messages`` — the SDK appends ``/v1/messages``.

Registry ``bedrock_claude`` models use a separate gateway path:
``POST {endpoint}/model/{deployment}/invoke`` (raw httpx, Bedrock payload).

Registry OpenAI-family models use Azure or OpenAI-compatible SDK base URLs.
"""
from __future__ import annotations

from .registry import ModelSpec

ANTHROPIC_MESSAGES_PATH = "/v1/messages"
ANTHROPIC_DIRECT_HOST = "https://api.anthropic.com"
BEDROCK_ANTHROPIC_VERSION = "bedrock-2023-05-31"


def anthropic_direct_messages_url() -> str:
    """Full URL for direct API-key Anthropic calls (LangChain / SDK default)."""
    return f"{ANTHROPIC_DIRECT_HOST}{ANTHROPIC_MESSAGES_PATH}"


def anthropic_gateway_sdk_base(endpoint: str) -> str:
    """Anthropic SDK ``base_url`` when routing through the UHG gateway.

    The SDK appends ``/v1/messages``; the gateway expects the ``/anthropic`` sub-route.
    """
    base = endpoint.rstrip("/")
    if base.endswith("/anthropic"):
        return base
    return f"{base}/anthropic"


def anthropic_gateway_messages_url(endpoint: str) -> str:
    """Full messages URL for gateway Anthropic SDK routing (logging only)."""
    return f"{anthropic_gateway_sdk_base(endpoint)}{ANTHROPIC_MESSAGES_PATH}"


def bedrock_invoke_url(endpoint: str, deployment: str) -> str:
    """Registry bedrock_claude invoke URL (httpx, not Anthropic SDK)."""
    dep = (deployment or "").strip()
    return f"{endpoint.rstrip('/')}/model/{dep}/invoke"


def bedrock_invoke_url_for_spec(spec: ModelSpec) -> str:
    return bedrock_invoke_url(spec.endpoint, spec.deployment)


def azure_openai_chat_url(spec: ModelSpec) -> str:
    api_version = spec.api_version or "2025-01-01-preview"
    return (
        f"{spec.endpoint}/openai/deployments/{spec.deployment}/chat/completions"
        f"?api-version={api_version}"
    )


def openai_compat_chat_url(spec: ModelSpec) -> str:
    return f"{spec.endpoint}/v1/chat/completions"


def describe_registry_target(spec: ModelSpec, *, json_mode: bool = False) -> str:
    """Human-readable URL/method for logs and error messages."""
    if spec.kind == "azure_openai":
        return (
            f"POST {azure_openai_chat_url(spec)}"
            f" [registry={spec.name!r} kind=azure_openai json_mode={json_mode}]"
        )
    if spec.kind == "openai_compat":
        return (
            f"POST {openai_compat_chat_url(spec)}"
            f" deployment={spec.deployment!r}"
            f" [registry={spec.name!r} kind=openai_compat json_mode={json_mode}]"
        )
    if spec.kind == "bedrock_claude":
        return (
            f"POST {bedrock_invoke_url_for_spec(spec)}"
            f" deployment={spec.deployment!r}"
            f" [registry={spec.name!r} kind=bedrock_claude]"
        )
    return f"registry={spec.name!r} kind={spec.kind!r} endpoint={spec.endpoint!r}"


def describe_api_key_target(*, provider: str, model: str = "") -> str:
    """Human-readable target for direct API-key (non-registry) calls."""
    prov = provider.strip().lower()
    if prov == "anthropic":
        suffix = f" model={model!r}" if model else ""
        return f"POST {anthropic_direct_messages_url()}{suffix} [backend=api_key]"
    if prov == "openai":
        suffix = f" model={model!r}" if model else ""
        return f"POST https://api.openai.com/v1/chat/completions{suffix} [backend=api_key]"
    return f"backend=api_key provider={provider!r}"
