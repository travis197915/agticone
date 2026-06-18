"""Invoke models through the UHG AI gateway (MODEL_REGISTRY)."""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from .registry import JSON_COMPATIBLE_KINDS, ModelSpec

log = logging.getLogger(__name__)

# Env vars checked in order for gateway authentication.
GATEWAY_KEY_ENV_VARS = (
    "AI_GATEWAY_API_KEY",
    "APIM_SUBSCRIPTION_KEY",
    "OPENAI_API_KEY",
)


def gateway_api_key_source() -> str:
    """Return the env var name that supplies the gateway key, or '' if unset."""
    for name in GATEWAY_KEY_ENV_VARS:
        if os.environ.get(name, "").strip():
            return name
    return ""


def gateway_api_key_configured() -> bool:
    return bool(gateway_api_key_source())


def _gateway_api_key() -> str:
    source = gateway_api_key_source()
    if source:
        return os.environ.get(source, "").strip()
    raise RuntimeError(
        "Registry backend requires one of: "
        + ", ".join(GATEWAY_KEY_ENV_VARS)
    )


def _gateway_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    project_id = os.environ.get("PROJECT_ID", "").strip()
    if project_id:
        headers["project-id"] = project_id
        headers["x-project-id"] = project_id
    # UHG gateway sits behind Azure APIM; subscription key is required alongside api-key.
    for name in ("AI_GATEWAY_API_KEY", "APIM_SUBSCRIPTION_KEY"):
        val = os.environ.get(name, "").strip()
        if val:
            headers["Ocp-Apim-Subscription-Key"] = val
            break
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


def supports_json_mode(spec: ModelSpec) -> bool:
    return spec.kind in JSON_COMPATIBLE_KINDS


def describe_registry_target(spec: ModelSpec, *, json_mode: bool = False) -> str:
    """Human-readable URL/method for logs and error messages."""
    if spec.kind == "azure_openai":
        api_version = spec.api_version or "2025-01-01-preview"
        return (
            f"POST {spec.endpoint}/openai/deployments/{spec.deployment}/chat/completions"
            f"?api-version={api_version}"
            f" [registry={spec.name!r} kind=azure_openai json_mode={json_mode}]"
        )
    if spec.kind == "openai_compat":
        return (
            f"POST {spec.endpoint}/v1/chat/completions"
            f" deployment={spec.deployment!r}"
            f" [registry={spec.name!r} kind=openai_compat json_mode={json_mode}]"
        )
    if spec.kind == "bedrock_claude":
        return (
            f"POST {spec.endpoint}/v1/messages"
            f" deployment={spec.deployment!r}"
            f" [registry={spec.name!r} kind=bedrock_claude]"
        )
    return f"registry={spec.name!r} kind={spec.kind!r} endpoint={spec.endpoint!r}"


def _log_gateway_context(*, agent_name: str, spec: ModelSpec, json_mode: bool) -> str:
    """Build one log line prefix with endpoint + auth context (no secrets)."""
    target = describe_registry_target(spec, json_mode=json_mode)
    auth = gateway_api_key_source() or "missing"
    project = "set" if os.environ.get("PROJECT_ID", "").strip() else "unset"
    agent = f"agent={agent_name} " if agent_name else ""
    return f"{agent}{target} auth={auth} project_id={project}"


def invoke_registry_model(
    spec: ModelSpec,
    *,
    prompt: str,
    max_tokens: int,
    json_mode: bool,
    agent_name: str = "",
) -> tuple[str, int, int]:
    """Call a registry model and return (content, prompt_tokens, completion_tokens)."""
    ctx = _log_gateway_context(agent_name=agent_name, spec=spec, json_mode=json_mode)
    log.info("llm_gateway request → %s max_tokens=%s", ctx, max_tokens)
    t0 = time.time()
    try:
        if spec.kind == "azure_openai":
            result = _invoke_azure_openai(
                spec, prompt=prompt, max_tokens=max_tokens, json_mode=json_mode,
            )
        elif spec.kind == "openai_compat":
            result = _invoke_openai_compat(
                spec, prompt=prompt, max_tokens=max_tokens, json_mode=json_mode,
            )
        elif spec.kind == "bedrock_claude":
            result = _invoke_bedrock_claude(
                spec, prompt=prompt, max_tokens=max_tokens,
            )
        else:
            raise RuntimeError(
                f"Unsupported MODEL_REGISTRY kind {spec.kind!r} for {spec.name!r}"
            )
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        log.warning(
            "llm_gateway failed (%sms) → %s error=%s",
            ms, ctx, exc,
        )
        raise

    ms = int((time.time() - t0) * 1000)
    log.info("llm_gateway ok (%sms) → %s", ms, ctx)
    return result


def invoke_registry_messages(
    spec: ModelSpec,
    *,
    content_blocks: list[dict[str, Any]],
    max_tokens: int,
    agent_name: str = "",
) -> tuple[str, int, int]:
    """Multimodal registry call (PDF document blocks) via bedrock_claude gateway."""
    if spec.kind != "bedrock_claude":
        raise RuntimeError(
            f"Multimodal PDF calls require a bedrock_claude registry model; "
            f"got kind={spec.kind!r} for registry key {spec.name!r}. "
            f"Set AGENT_MODEL_MAP pdf_page_reader=opus (or LLM_MODEL=opus)."
        )
    ctx = _log_gateway_context(agent_name=agent_name, spec=spec, json_mode=False)
    log.info("llm_gateway pdf request → %s max_tokens=%s blocks=%d",
             ctx, max_tokens, len(content_blocks))
    t0 = time.time()
    try:
        from anthropic import Anthropic

        client = Anthropic(
            api_key=_gateway_api_key(),
            base_url=f"{spec.endpoint}/v1",
            default_headers=_gateway_headers(),
        )
        resp = client.messages.create(
            model=spec.deployment,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content_blocks}],
            temperature=0,
        )
        parts: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        inp, out = _usage_from_anthropic(resp)
        result = "".join(parts), inp, out
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        log.warning(
            "llm_gateway pdf failed (%sms) → %s error=%s",
            ms, ctx, exc,
        )
        raise

    ms = int((time.time() - t0) * 1000)
    log.info("llm_gateway pdf ok (%sms) → %s", ms, ctx)
    return result


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
        base_url=f"{spec.endpoint}/v1",
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
