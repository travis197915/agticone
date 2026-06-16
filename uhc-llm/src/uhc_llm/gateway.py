"""Invoke models through the UHG AI gateway (MODEL_REGISTRY)."""
from __future__ import annotations

import os
from typing import Any

from .registry import JSON_COMPATIBLE_KINDS, ModelSpec


def _gateway_api_key() -> str:
    for name in ("AI_GATEWAY_API_KEY", "APIM_SUBSCRIPTION_KEY", "OPENAI_API_KEY"):
        val = os.environ.get(name, "").strip()
        if val:
            return val
    raise RuntimeError(
        "Registry backend requires AI_GATEWAY_API_KEY, APIM_SUBSCRIPTION_KEY, "
        "or OPENAI_API_KEY"
    )


def _gateway_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    project_id = os.environ.get("PROJECT_ID", "").strip()
    if project_id:
        headers["project-id"] = project_id
        headers["x-project-id"] = project_id
    return headers


def _usage_from_openai(resp: Any) -> tuple[int, int]:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "prompt_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or 0),
    )


def _usage_from_anthropic(resp: Any) -> tuple[int, int]:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


def invoke_registry_model(
    spec: ModelSpec,
    *,
    prompt: str,
    max_tokens: int,
    json_mode: bool,
) -> tuple[str, int, int]:
    """Call a registry model and return (content, prompt_tokens, completion_tokens)."""
    if spec.kind == "azure_openai":
        return _invoke_azure_openai(spec, prompt=prompt, max_tokens=max_tokens, json_mode=json_mode)
    if spec.kind == "openai_compat":
        return _invoke_openai_compat(spec, prompt=prompt, max_tokens=max_tokens, json_mode=json_mode)
    if spec.kind == "bedrock_claude":
        return _invoke_bedrock_claude(spec, prompt=prompt, max_tokens=max_tokens)
    raise RuntimeError(f"Unsupported MODEL_REGISTRY kind {spec.kind!r} for {spec.name!r}")


def supports_json_mode(spec: ModelSpec) -> bool:
    return spec.kind in JSON_COMPATIBLE_KINDS


def _invoke_azure_openai(
    spec: ModelSpec,
    *,
    prompt: str,
    max_tokens: int,
    json_mode: bool,
) -> tuple[str, int, int]:
    from openai import AzureOpenAI

    api_version = spec.api_version or "2025-01-01-preview"
    client = AzureOpenAI(
        api_key=_gateway_api_key(),
        azure_endpoint=spec.endpoint,
        api_version=api_version,
        default_headers=_gateway_headers(),
    )
    kwargs: dict[str, Any] = {
        "model": spec.deployment,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content if resp.choices else ""
    inp, out = _usage_from_openai(resp)
    return content or "", inp, out


def _invoke_openai_compat(
    spec: ModelSpec,
    *,
    prompt: str,
    max_tokens: int,
    json_mode: bool,
) -> tuple[str, int, int]:
    from openai import OpenAI

    client = OpenAI(
        api_key=_gateway_api_key(),
        base_url=f"{spec.endpoint}/v1",
        default_headers=_gateway_headers(),
    )
    kwargs: dict[str, Any] = {
        "model": spec.deployment,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content if resp.choices else ""
    inp, out = _usage_from_openai(resp)
    return content or "", inp, out


def _invoke_bedrock_claude(
    spec: ModelSpec,
    *,
    prompt: str,
    max_tokens: int,
) -> tuple[str, int, int]:
    from anthropic import Anthropic

    client = Anthropic(
        api_key=_gateway_api_key(),
        base_url=spec.endpoint,
        default_headers=_gateway_headers(),
    )
    resp = client.messages.create(
        model=spec.deployment,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    parts: list[str] = []
    for block in resp.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    inp, out = _usage_from_anthropic(resp)
    return "".join(parts), inp, out
