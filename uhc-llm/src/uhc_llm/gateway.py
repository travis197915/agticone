"""Invoke models through the UHG AI gateway (MODEL_REGISTRY)."""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from .bedrock_http import build_bedrock_body, parse_bedrock_response, post_json_with_retries
from .messages import normalize_chat_messages, split_anthropic_system_messages
from .registry import JSON_COMPATIBLE_KINDS, ModelSpec
from .routes import describe_registry_target

log = logging.getLogger(__name__)

# Re-export for backward compatibility.
__all__ = [
    "GATEWAY_KEY_ENV_VARS",
    "describe_registry_target",
    "gateway_api_key_configured",
    "gateway_api_key_source",
    "gateway_auth_configured",
    "gateway_auth_source",
    "invoke_registry_chat",
    "invoke_registry_messages",
    "invoke_registry_model",
    "supports_json_mode",
]

# Env vars checked in order for static gateway API-key authentication.
GATEWAY_KEY_ENV_VARS = (
    "AI_GATEWAY_API_KEY",
    "APIM_SUBSCRIPTION_KEY",
    "OPENAI_API_KEY",
)


def _gateway_timeout(default: float) -> float:
    """HTTP read timeout (seconds) for gateway invoke calls; env-tunable.

    A ``bedrock_claude`` text generation with a large ``max_tokens`` (e.g. a
    ~100-step executive summary) can take longer than the old hard 60s and trip
    ``ReadTimeout`` → fallback. Default is raised to 180s and can be pushed
    higher via ``LLM_GATEWAY_TIMEOUT``.
    """
    raw = os.environ.get("LLM_GATEWAY_TIMEOUT", "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return default


def gateway_api_key_source() -> str:
    """Return the env var name that supplies a static API key, or '' if unset."""
    for name in GATEWAY_KEY_ENV_VARS:
        if os.environ.get(name, "").strip():
            return name
    return ""


def gateway_api_key_configured() -> bool:
    return bool(gateway_api_key_source())


def gateway_auth_configured() -> bool:
    """True when OAuth client-credentials or a static API key is configured."""
    from .oauth import oauth_configured

    return oauth_configured() or gateway_api_key_configured()


def gateway_auth_source() -> str:
    """Human-readable auth mode for logs (never includes secrets)."""
    from .oauth import oauth_configured

    if oauth_configured():
        return "oauth:client_credentials"
    source = gateway_api_key_source()
    return source or "missing"


def _gateway_api_key() -> str:
    """Static APIM / gateway subscription key (api_key mode only)."""
    source = gateway_api_key_source()
    if source:
        return os.environ.get(source, "").strip()
    raise RuntimeError(
        "Registry backend requires OAuth (AUTH_URL, CLIENT_ID, CLIENT_SECRET, SCOPE) "
        "or one of: " + ", ".join(GATEWAY_KEY_ENV_VARS)
    )


def _gateway_bearer_token() -> str:
    from .oauth import fetch_oauth_token

    return fetch_oauth_token()


def _client_api_key() -> str:
    """Value passed as ``api_key=`` on OpenAI / Anthropic SDK clients."""
    from .oauth import oauth_configured

    if oauth_configured():
        return _gateway_bearer_token()
    return _gateway_api_key()


def _gateway_headers(*, oauth_bearer: bool = True) -> dict[str, str]:
    from .oauth import oauth_configured

    headers: dict[str, str] = {}
    project_id = os.environ.get("PROJECT_ID", "").strip()
    if project_id:
        headers["projectId"] = project_id
        headers["project-id"] = project_id
        headers["x-project-id"] = project_id

    if oauth_configured():
        if oauth_bearer:
            headers["Authorization"] = f"Bearer {_gateway_bearer_token()}"
    else:
        for name in ("AI_GATEWAY_API_KEY", "APIM_SUBSCRIPTION_KEY"):
            val = os.environ.get(name, "").strip()
            if val:
                headers["Ocp-Apim-Subscription-Key"] = val
                break
    return headers


def _bedrock_request_headers(spec: ModelSpec) -> dict[str, str]:
    return {
        **_gateway_headers(),
        "deployment-id": spec.deployment,
        "Content-Type": "application/json",
    }


def _usage_from_openai(resp: Any) -> tuple[int, int]:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "prompt_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or 0),
    )


def supports_json_mode(spec: ModelSpec) -> bool:
    return spec.kind in JSON_COMPATIBLE_KINDS


def _log_gateway_context(*, agent_name: str, spec: ModelSpec, json_mode: bool) -> str:
    """Build one log line prefix with endpoint + auth context (no secrets)."""
    target = describe_registry_target(spec, json_mode=json_mode)
    auth = gateway_auth_source()
    project = "set" if os.environ.get("PROJECT_ID", "").strip() else "unset"
    agent = f"agent={agent_name} " if agent_name else ""
    return f"{agent}{target} auth={auth} project_id={project}"


def _format_gateway_error(exc: Exception) -> str:
    err = str(exc)
    if "access_token is missing" in err and gateway_auth_source() != "oauth:client_credentials":
        err += (
            " — UHG gateway requires OAuth. Set AUTH_URL, CLIENT_ID, "
            "CLIENT_SECRET, and SCOPE in .env (not OPENAI_API_KEY)."
        )
    return err


def invoke_registry_chat(
    spec: ModelSpec,
    *,
    messages: list[dict[str, Any]],
    max_tokens: int,
    json_mode: bool,
    agent_name: str = "",
) -> tuple[str, int, int]:
    """Call a registry model with a chat ``messages`` list."""
    normalized = normalize_chat_messages(messages)
    ctx = _log_gateway_context(agent_name=agent_name, spec=spec, json_mode=json_mode)
    log.info(
        "llm_gateway request → %s max_tokens=%s messages=%d",
        ctx, max_tokens, len(normalized),
    )
    t0 = time.time()
    try:
        if spec.kind == "azure_openai":
            result = _invoke_azure_openai(
                spec, messages=normalized, max_tokens=max_tokens, json_mode=json_mode,
            )
        elif spec.kind == "openai_compat":
            result = _invoke_openai_compat(
                spec, messages=normalized, max_tokens=max_tokens, json_mode=json_mode,
            )
        elif spec.kind == "bedrock_claude":
            result = _invoke_bedrock_claude(
                spec, messages=normalized, max_tokens=max_tokens,
            )
        else:
            raise RuntimeError(
                f"Unsupported MODEL_REGISTRY kind {spec.kind!r} for {spec.name!r}"
            )
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        err = _format_gateway_error(exc)
        log.warning("llm_gateway failed (%sms) → %s error=%s", ms, ctx, err)
        raise RuntimeError(err) from exc

    ms = int((time.time() - t0) * 1000)
    log.info("llm_gateway ok (%sms) → %s", ms, ctx)
    return result


def invoke_registry_model(
    spec: ModelSpec,
    *,
    prompt: str,
    max_tokens: int,
    json_mode: bool,
    agent_name: str = "",
) -> tuple[str, int, int]:
    """Call a registry model with a single user prompt (legacy helper)."""
    return invoke_registry_chat(
        spec,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        json_mode=json_mode,
        agent_name=agent_name,
    )


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
    log.info(
        "llm_gateway pdf request → %s max_tokens=%s blocks=%d",
        ctx, max_tokens, len(content_blocks),
    )
    t0 = time.time()
    try:
        from .routes import bedrock_invoke_url_for_spec

        url = bedrock_invoke_url_for_spec(spec)
        body = build_bedrock_body(
            messages=[{"role": "user", "content": content_blocks}],
            max_tokens=max_tokens,
        )
        data = post_json_with_retries(
            url,
            headers=_bedrock_request_headers(spec),
            body=body,
            timeout=180.0,
            log_context=f"model={spec.name} deployment={spec.deployment}",
        )
        result = parse_bedrock_response(data)
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        err = _format_gateway_error(exc)
        log.warning("llm_gateway pdf failed (%sms) → %s error=%s", ms, ctx, err)
        raise RuntimeError(err) from exc

    ms = int((time.time() - t0) * 1000)
    log.info("llm_gateway pdf ok (%sms) → %s", ms, ctx)
    return result


def _invoke_azure_openai(
    spec: ModelSpec,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    json_mode: bool,
) -> tuple[str, int, int]:
    from openai import AzureOpenAI

    api_version = spec.api_version or "2025-01-01-preview"
    client = AzureOpenAI(
        api_key=_client_api_key(),
        azure_endpoint=spec.endpoint,
        api_version=api_version,
        default_headers=_gateway_headers(),
    )
    kwargs: dict[str, Any] = {
        "model": spec.deployment,
        "messages": messages,
    }
    deployment_name = (spec.deployment or "").lower()
    if "gpt-5" in deployment_name:
        kwargs["max_completion_tokens"] = max_tokens
        kwargs["reasoning_effort"] = "low"
    else:
        kwargs["max_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content if resp.choices else ""
    inp, out = _usage_from_openai(resp)
    if content:
        return content or "", inp, out

    try:
        input_items: list[dict[str, Any]] = []
        for m in messages:
            role = str(m.get("role", "user"))
            text = str(m.get("content", ""))
            input_items.append(
                {"role": role, "content": [{"type": "input_text", "text": text}]}
            )
        rkwargs: dict[str, Any] = {
            "model": spec.deployment,
            "input": input_items,
            "max_output_tokens": max_tokens,
        }
        rresp = client.responses.create(**rkwargs)
        out_text = getattr(rresp, "output_text", "") or ""
        if not out_text and getattr(rresp, "output", None):
            parts: list[str] = []
            for item in rresp.output:
                for c in getattr(item, "content", []) or []:
                    txt = getattr(c, "text", None)
                    if txt:
                        parts.append(str(txt))
            out_text = "".join(parts)
        usage = getattr(rresp, "usage", None)
        rin = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        rout = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        return out_text or "", rin, rout
    except Exception:
        return "", inp, out


def _invoke_openai_compat(
    spec: ModelSpec,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    json_mode: bool,
) -> tuple[str, int, int]:
    from openai import OpenAI

    client = OpenAI(
        api_key=_client_api_key(),
        base_url=f"{spec.endpoint}/v1",
        default_headers=_gateway_headers(),
    )
    kwargs: dict[str, Any] = {
        "model": spec.deployment,
        "messages": messages,
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
    messages: list[dict[str, str]],
    max_tokens: int,
) -> tuple[str, int, int]:
    from .routes import bedrock_invoke_url_for_spec

    system, chat_messages = split_anthropic_system_messages(messages)
    url = bedrock_invoke_url_for_spec(spec)
    body = build_bedrock_body(
        messages=chat_messages,
        max_tokens=max_tokens,
        system=system,
    )
    data = post_json_with_retries(
        url,
        headers=_bedrock_request_headers(spec),
        body=body,
        timeout=_gateway_timeout(180.0),
        log_context=f"model={spec.name} deployment={spec.deployment}",
    )
    return parse_bedrock_response(data)
