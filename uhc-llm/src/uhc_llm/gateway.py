"""Invoke models through the UHG AI gateway (MODEL_REGISTRY)."""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from .registry import JSON_COMPATIBLE_KINDS, ModelSpec

log = logging.getLogger(__name__)

# Env vars checked in order for static gateway API-key authentication.
GATEWAY_KEY_ENV_VARS = (
    "AI_GATEWAY_API_KEY",
    "APIM_SUBSCRIPTION_KEY",
    "OPENAI_API_KEY",
)


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
        # Different gateway paths expect different project-id header casing.
        headers["projectId"] = project_id
        headers["project-id"] = project_id
        headers["x-project-id"] = project_id

    if oauth_configured():
        if oauth_bearer:
            headers["Authorization"] = f"Bearer {_gateway_bearer_token()}"
    else:
        # UHG gateway sits behind Azure APIM; subscription key header when using api_key mode.
        for name in ("AI_GATEWAY_API_KEY", "APIM_SUBSCRIPTION_KEY"):
            val = os.environ.get(name, "").strip()
            if val:
                headers["Ocp-Apim-Subscription-Key"] = val
                break
    return headers


def _anthropic_base_url(endpoint: str) -> str:
    """Return gateway root without appending Anthropic sub-paths (SDK adds ``/v1/messages``)."""
    return endpoint.rstrip("/")


def _bedrock_invoke_url(spec: ModelSpec) -> str:
    base = spec.endpoint.rstrip("/")
    deployment = (spec.deployment or "").strip()
    return f"{base}/model/{deployment}/invoke"


def _anthropic_model_candidates(deployment: str) -> list[str]:
    """Return model/deployment candidates compatible with tenant gateway naming."""
    raw = (deployment or "").strip()
    if not raw:
        return [raw]

    candidates: list[str] = [raw]
    # Common Bedrock deployment form: us.anthropic.claude-opus-4-6-v1
    simplified = raw
    if simplified.startswith("us.anthropic."):
        simplified = simplified[len("us.anthropic."):]
        candidates.append(simplified)
    if simplified.endswith("-v1"):
        candidates.append(simplified[:-3])

    # Preserve order while removing duplicates.
    seen: set[str] = set()
    ordered: list[str] = []
    for name in candidates:
        if name and name not in seen:
            ordered.append(name)
            seen.add(name)
    return ordered or [raw]


def _anthropic_gateway_client(spec: ModelSpec):
    """Build Anthropic client for bedrock_claude gateway (OAuth or API key)."""
    from anthropic import Anthropic
    from .oauth import oauth_configured

    base_url = _anthropic_base_url(spec.endpoint)
    if oauth_configured():
        # Gateway expects Authorization: Bearer (not x-api-key).
        return Anthropic(
            auth_token=_gateway_bearer_token(),
            base_url=base_url,
            default_headers=_gateway_headers(oauth_bearer=False),
        )
    return Anthropic(
        api_key=_gateway_api_key(),
        base_url=base_url,
        default_headers=_gateway_headers(oauth_bearer=False),
    )


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
            f"POST {_bedrock_invoke_url(spec)}"
            f" deployment={spec.deployment!r}"
            f" [registry={spec.name!r} kind=bedrock_claude]"
        )
    return f"registry={spec.name!r} kind={spec.kind!r} endpoint={spec.endpoint!r}"


def _log_gateway_context(*, agent_name: str, spec: ModelSpec, json_mode: bool) -> str:
    """Build one log line prefix with endpoint + auth context (no secrets)."""
    target = describe_registry_target(spec, json_mode=json_mode)
    auth = gateway_auth_source()
    project = "set" if os.environ.get("PROJECT_ID", "").strip() else "unset"
    agent = f"agent={agent_name} " if agent_name else ""
    return f"{agent}{target} auth={auth} project_id={project}"


def _normalize_messages(
    messages: list[dict[str, Any]] | None,
    *,
    prompt: str = "",
) -> list[dict[str, str]]:
    if messages:
        return [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in messages
        ]
    if prompt:
        return [{"role": "user", "content": prompt}]
    return [{"role": "user", "content": ""}]


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
    normalized = _normalize_messages(messages)
    ctx = _log_gateway_context(agent_name=agent_name, spec=spec, json_mode=json_mode)
    log.info("llm_gateway request → %s max_tokens=%s messages=%d",
             ctx, max_tokens, len(normalized))
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
        log.warning(
            "llm_gateway failed (%sms) → %s error=%s",
            ms, ctx, err,
        )
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
    log.info("llm_gateway pdf request → %s max_tokens=%s blocks=%d",
             ctx, max_tokens, len(content_blocks))
    t0 = time.time()
    try:
        headers = {
            **_gateway_headers(),
            "deployment-id": spec.deployment,
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "messages": [{"role": "user", "content": content_blocks}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        url = _bedrock_invoke_url(spec)
        max_retries = 4
        base_delay = 1.0
        last_exc: Exception | None = None
        http_resp = None

        for attempt in range(max_retries + 1):
            with httpx.Client(timeout=180.0) as client:
                http_resp = client.post(url, headers=headers, json=body)

            if http_resp.status_code == 200:
                break

            retryable = http_resp.status_code == 429 or http_resp.status_code >= 500
            log.warning(
                "llm_api_error model=%s deployment=%s status=%d url=%s attempt=%d/%d retryable=%s body=%s",
                spec.name,
                spec.deployment,
                http_resp.status_code,
                url,
                attempt + 1,
                max_retries + 1,
                retryable,
                http_resp.text[:500],
            )
            if retryable and attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                retry_after = http_resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass
                time.sleep(delay)
                continue
            last_exc = httpx.HTTPStatusError(
                message=f"Client error '{http_resp.status_code}' for url '{url}'",
                request=http_resp.request,
                response=http_resp,
            )
            break

        if last_exc is not None:
            raise last_exc
        if http_resp is None:
            raise RuntimeError(f"Bedrock PDF invoke failed without a response for {url}")

        data: dict[str, Any] = http_resp.json()
        parts: list[str] = []
        for block in (data.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if text:
                    parts.append(str(text))
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        inp = int(usage.get("input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        result = "".join(parts), inp, out
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        err = _format_gateway_error(exc)
        log.warning(
            "llm_gateway pdf failed (%sms) → %s error=%s",
            ms, ctx, err,
        )
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

    # Some GPT-5 gateway deployments return empty content via chat.completions.
    # Fall back to Responses API and use output_text when available.
    try:
        input_items: list[dict[str, Any]] = []
        for m in messages:
            role = str(m.get("role", "user"))
            text = str(m.get("content", ""))
            input_items.append(
                {
                    "role": role,
                    "content": [{"type": "input_text", "text": text}],
                }
            )
        rkwargs: dict[str, Any] = {
            "model": spec.deployment,
            "input": input_items,
        }
        if "gpt-5" in deployment_name:
            rkwargs["max_output_tokens"] = max_tokens
        else:
            rkwargs["max_output_tokens"] = max_tokens
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


def _split_anthropic_messages(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[dict[str, str]]]:
    """Move ``system`` role content to Anthropic's ``system`` parameter."""
    system_parts: list[str] = []
    chat: list[dict[str, str]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        chat.append({"role": role, "content": content})
    if not chat:
        chat = [{"role": "user", "content": ""}]
    system = "\n\n".join(system_parts) if system_parts else None
    return system, chat


def _invoke_bedrock_claude(
    spec: ModelSpec,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> tuple[str, int, int]:
    system, chat_messages = _split_anthropic_messages(messages)
    headers = {
        **_gateway_headers(),
        "deployment-id": spec.deployment,
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "anthropic_version": "bedrock-2023-05-31",
        "messages": chat_messages,
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    if system:
        body["system"] = system

    url = _bedrock_invoke_url(spec)
    max_retries = 4
    base_delay = 1.0
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, headers=headers, json=body)

        if resp.status_code == 200:
            data: dict[str, Any] = resp.json()
            if "choices" in data:
                choices = data.get("choices") or []
                if choices:
                    message = choices[0].get("message") or {}
                    content = message.get("content") or ""
                    return str(content), 0, 0

            if "content" in data and isinstance(data["content"], list):
                parts: list[str] = []
                for block in data["content"]:
                    if isinstance(block, dict):
                        text = block.get("text")
                        if text:
                            parts.append(str(text))
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                inp = int(usage.get("input_tokens", 0) or 0)
                out = int(usage.get("output_tokens", 0) or 0)
                return "".join(parts), inp, out

            return "", 0, 0

        retryable = resp.status_code == 429 or resp.status_code >= 500
        log.warning(
            "llm_api_error model=%s deployment=%s status=%d url=%s attempt=%d/%d retryable=%s body=%s",
            spec.name,
            spec.deployment,
            resp.status_code,
            url,
            attempt + 1,
            max_retries + 1,
            retryable,
            resp.text[:500],
        )

        if retryable and attempt < max_retries:
            delay = base_delay * (2 ** attempt)
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
            time.sleep(delay)
            continue

        last_exc = httpx.HTTPStatusError(
            message=f"Client error '{resp.status_code}' for url '{url}'",
            request=resp.request,
            response=resp,
        )
        break

    if last_exc is not None:
        raise last_exc

    raise RuntimeError(f"Bedrock invoke failed without a captured error for {url}")
