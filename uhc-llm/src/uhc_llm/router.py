"""Unified LLM entrypoint: api_key (LangChain) or registry (gateway)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .messages import extract_pdf_text_from_b64, pdf_document_blocks
from .prompts import enrich_for_json
from .routes import describe_api_key_target, describe_registry_target


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


def describe_target(
    agent_name: str,
    *,
    pdf: bool = False,
    json_mode: bool = False,
    model_name: str | None = None,
    provider: str = "anthropic",
    cfg: _ApiKeyCfg | None = None,
) -> str:
    """Human-readable invoke target for logs (registry or api_key)."""
    from .backend import get_llm_backend

    if get_llm_backend() == "registry":
        from .registry import get_model_spec, resolve_pdf_registry_model_name, resolve_registry_model_name

        resolved = model_name or (
            resolve_pdf_registry_model_name(agent_name)
            if pdf
            else resolve_registry_model_name(agent_name)
        )
        return describe_registry_target(
            get_model_spec(resolved),
            json_mode=json_mode,
        )

    model = model_name
    if not model and cfg is not None:
        model = (
            getattr(cfg, "anthropic_model", "")
            if provider == "anthropic"
            else getattr(cfg, "openai_model", "")
        )
    return describe_api_key_target(provider=provider, model=model or "")


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
    """Invoke an LLM using the active backend (single user prompt)."""
    return invoke_chat(
        agent_name=agent_name,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        json_mode=json_mode,
        provider=provider,
        cfg=cfg,
        model_name=model_name,
    )


def invoke_chat(
    *,
    agent_name: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 4096,
    json_mode: bool = False,
    provider: str = "anthropic",
    cfg: _ApiKeyCfg | None = None,
    model_name: str | None = None,
) -> LLMResponse:
    """Invoke a chat ``messages`` list on the active backend."""
    from .backend import get_llm_backend

    if get_llm_backend() == "registry":
        return _invoke_registry_chat(
            agent_name=agent_name,
            messages=messages,
            max_tokens=max_tokens,
            json_mode=json_mode,
            model_name=model_name,
        )
    return _invoke_api_key_chat(
        messages=messages,
        max_tokens=max_tokens,
        json_mode=json_mode,
        provider=provider,
        cfg=cfg,
    )


def invoke_pdf(
    *,
    agent_name: str,
    prompt: str,
    pdf_b64_list: list[str],
    max_tokens: int = 8192,
    cfg: _ApiKeyCfg | None = None,
    model_name: str | None = None,
) -> LLMResponse:
    """Multimodal PDF call on the active backend.

    Registry ``bedrock_claude`` models use native document blocks via the
    bedrock invoke route. Other registry models and all api_key calls fall
    back to extracted PDF text in the user prompt.
    """
    from .backend import get_llm_backend

    if get_llm_backend() == "registry":
        return _invoke_registry_pdf(
            agent_name=agent_name,
            prompt=prompt,
            pdf_b64_list=pdf_b64_list,
            max_tokens=max_tokens,
            model_name=model_name,
        )
    return _invoke_api_key_pdf(
        prompt=prompt,
        pdf_b64_list=pdf_b64_list,
        max_tokens=max_tokens,
        cfg=cfg,
    )


def _invoke_registry_chat(
    *,
    agent_name: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    json_mode: bool,
    model_name: str | None,
) -> LLMResponse:
    from .gateway import invoke_registry_chat, supports_json_mode
    from .registry import get_model_spec, resolve_registry_model_name

    resolved = model_name or resolve_registry_model_name(agent_name)
    spec = get_model_spec(resolved)
    use_json = json_mode and supports_json_mode(spec)
    content, inp, out = invoke_registry_chat(
        spec,
        messages=messages,
        max_tokens=max_tokens,
        json_mode=use_json,
        agent_name=agent_name,
    )
    return LLMResponse(
        content=content,
        provider=f"registry:{spec.kind}",
        model=resolved,
        prompt_tokens=inp,
        completion_tokens=out,
    )


def _invoke_registry_pdf(
    *,
    agent_name: str,
    prompt: str,
    pdf_b64_list: list[str],
    max_tokens: int,
    model_name: str | None,
) -> LLMResponse:
    from .gateway import invoke_registry_chat, invoke_registry_messages
    from .registry import get_model_spec, resolve_pdf_registry_model_name

    resolved = model_name or resolve_pdf_registry_model_name(agent_name)
    spec = get_model_spec(resolved)
    if spec.kind == "bedrock_claude":
        blocks = pdf_document_blocks(prompt, pdf_b64_list)
        content, inp, out = invoke_registry_messages(
            spec,
            content_blocks=blocks,
            max_tokens=max_tokens,
            agent_name=agent_name,
        )
    else:
        extracted = extract_pdf_text_from_b64(pdf_b64_list)
        if not extracted:
            raise RuntimeError(
                "Unable to extract text from PDF slices for non-bedrock registry model"
            )
        text_prompt = (
            f"{prompt}\n\nPDF_TEXT_EXTRACT (from attached slices):\n{extracted}"
        )
        content, inp, out = invoke_registry_chat(
            spec,
            messages=[{"role": "user", "content": text_prompt}],
            max_tokens=max_tokens,
            json_mode=False,
            agent_name=agent_name,
        )
    return LLMResponse(
        content=content,
        provider=f"registry:{spec.kind}",
        model=resolved,
        prompt_tokens=inp,
        completion_tokens=out,
    )


def _invoke_api_key_chat(
    *,
    messages: list[dict[str, Any]],
    max_tokens: int,
    json_mode: bool,
    provider: str,
    cfg: _ApiKeyCfg | None,
) -> LLMResponse:
    from langchain_core.messages import HumanMessage, SystemMessage

    chat_messages: list[Any] = []
    for msg in messages:
        role = str(msg.get("role", "user"))
        content = str(msg.get("content", ""))
        if role == "system":
            chat_messages.append(SystemMessage(content=content))
        else:
            chat_messages.append(HumanMessage(content=content))

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

    resp = llm.invoke(chat_messages)
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


def _invoke_api_key_pdf(
    *,
    prompt: str,
    pdf_b64_list: list[str],
    max_tokens: int,
    cfg: _ApiKeyCfg | None,
) -> LLMResponse:
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage

    model = getattr(cfg, "anthropic_model", None) if cfg else None
    api_key = getattr(cfg, "anthropic_api_key", None) if cfg else None
    llm = ChatAnthropic(
        model=model or _env_model("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
        api_key=api_key or _env("ANTHROPIC_API_KEY"),
        max_tokens=max_tokens,
        temperature=0,
    )
    content_blocks = pdf_document_blocks(prompt, pdf_b64_list)
    resp = llm.invoke([HumanMessage(content=content_blocks)])
    usage = getattr(resp, "usage_metadata", None) or {}
    inp = int(usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or usage.get("completion_tokens", 0) or 0)
    text = resp.content if isinstance(resp.content, str) else _coerce_text_content(resp.content)
    return LLMResponse(
        content=text,
        provider="anthropic",
        model=str(llm.model),
        prompt_tokens=inp,
        completion_tokens=out,
    )


def _coerce_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict):
                parts.append(blk.get("text", "") or "")
            else:
                parts.append(str(blk))
        return "".join(parts)
    return str(content)


def _env(name: str) -> str:
    import os
    return os.environ.get(name, "").strip()


def _env_model(name: str, default: str) -> str:
    val = _env(name)
    return val or default
