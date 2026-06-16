"""Unified LLM entrypoint: api_key (LangChain) or registry (gateway)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class _ApiKeyCfg(Protocol):
    openai_api_key: str
    anthropic_api_key: str
    openai_model: str
    anthropic_model: str


@dataclass(frozen=True)
class LLMResponse:
    content: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int


def invoke_prompt(
    *,
    agent_name: str,
    prompt: str,
    max_tokens: int = 4096,
    json_mode: bool = False,
    provider: str = "anthropic",
    cfg: _ApiKeyCfg | None = None,
    model_name: str | None = None,
) -> LLMResponse:
    """Invoke an LLM using the active backend.

    ``registry`` backend uses ``LLM_MODEL`` (app-wide) or optional
    ``AGENT_MODEL_MAP`` to pick a key from ``MODEL_REGISTRY``, then calls
    the UHG gateway. ``model_name`` overrides both when supplied.

    ``api_key`` backend uses LangChain ChatAnthropic / ChatOpenAI with keys
    from ``cfg`` (or standard env vars when cfg is omitted).
    """
    from .backend import get_llm_backend

    if get_llm_backend() == "registry":
        return _invoke_registry(
            agent_name=agent_name,
            prompt=prompt,
            max_tokens=max_tokens,
            json_mode=json_mode,
            model_name=model_name,
        )
    return _invoke_api_key(
        prompt=prompt,
        max_tokens=max_tokens,
        json_mode=json_mode,
        provider=provider,
        cfg=cfg,
    )


def _invoke_registry(
    *,
    agent_name: str,
    prompt: str,
    max_tokens: int,
    json_mode: bool,
    model_name: str | None,
) -> LLMResponse:
    from .gateway import invoke_registry_model, supports_json_mode
    from .registry import get_model_spec, resolve_registry_model_name

    resolved = model_name or resolve_registry_model_name(agent_name)
    spec = get_model_spec(resolved)
    use_json = json_mode and supports_json_mode(spec)
    content, inp, out = invoke_registry_model(
        spec,
        prompt=prompt,
        max_tokens=max_tokens,
        json_mode=use_json,
    )
    return LLMResponse(
        content=content,
        provider=f"registry:{spec.kind}",
        model=resolved,
        prompt_tokens=inp,
        completion_tokens=out,
    )


def _invoke_api_key(
    *,
    prompt: str,
    max_tokens: int,
    json_mode: bool,
    provider: str,
    cfg: _ApiKeyCfg | None,
) -> LLMResponse:
    from langchain_core.messages import HumanMessage

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        model = getattr(cfg, "anthropic_model", None) if cfg else None
        api_key = getattr(cfg, "anthropic_api_key", None) if cfg else None
        llm = ChatAnthropic(
            model=model or _env_model("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
            api_key=api_key or _env("ANTHROPIC_API_KEY"),
            max_tokens=max_tokens,
            temperature=0,
        )
        prov_name = "anthropic"
        model_name = llm.model
    else:
        from langchain_openai import ChatOpenAI

        model = getattr(cfg, "openai_model", None) if cfg else None
        api_key = getattr(cfg, "openai_api_key", None) if cfg else None
        kwargs: dict[str, Any] = {}
        if json_mode:
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
        llm = ChatOpenAI(
            model=model or _env_model("OPENAI_MODEL", "gpt-4o"),
            api_key=api_key or _env("OPENAI_API_KEY"),
            max_tokens=max_tokens,
            temperature=0,
            **kwargs,
        )
        prov_name = "openai"
        model_name = llm.model_name

    resp = llm.invoke([HumanMessage(content=prompt)])
    usage = getattr(resp, "usage_metadata", None) or {}
    inp = int(usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or usage.get("completion_tokens", 0) or 0)
    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    return LLMResponse(
        content=content,
        provider=prov_name,
        model=str(model_name),
        prompt_tokens=inp,
        completion_tokens=out,
    )


def _env(name: str) -> str:
    import os
    return os.environ.get(name, "").strip()


def _env_model(name: str, default: str) -> str:
    val = _env(name)
    return val or default
