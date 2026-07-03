"""Select direct API-key clients vs MODEL_REGISTRY gateway routing."""
from __future__ import annotations

import os


def _registry_configured() -> bool:
    return any(
        os.environ.get(name, "").strip()
        for name in ("MODEL_REGISTRY_JSON", "MODEL_REGISTRY_FILE", "MODEL_REGISTRY")
    )


def get_llm_backend() -> str:
    """Return ``api_key`` or ``registry``.

    Resolution order:
    1. Explicit ``LLM_BACKEND=api_key|registry``
    2. If any registry source is configured → ``registry``
    3. Default → ``api_key``

    Registry mode — keep sensitive gateway endpoints out of git::

        LLM_BACKEND=registry
        MODEL_REGISTRY_JSON='{"gpt-5-mini":{"kind":"azure_openai",...}}'
        LLM_MODEL=gpt-5-mini

    Alternatives for the model list (first match wins):

    - ``MODEL_REGISTRY_JSON`` — full JSON string in ``.env`` (recommended)
    - ``MODEL_REGISTRY_FILE`` — path to a local gitignored JSON file
    - ``MODEL_REGISTRY`` — inline JSON, profile name, or file path

    Copy ``config/registries/uhg-gateway.example.json`` locally if you prefer
    a file over a long env string.
    """
    explicit = os.environ.get("LLM_BACKEND", "").strip().lower()
    if explicit in {"api_key", "registry"}:
        return explicit
    if _registry_configured():
        return "registry"
    return "api_key"


def is_registry_backend() -> bool:
    return get_llm_backend() == "registry"


_DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"
_DEFAULT_OPENAI_MODEL = "gpt-4o"

# Public constants — single source of truth for api_key fallback model ids.
DEFAULT_ANTHROPIC_MODEL = _DEFAULT_ANTHROPIC_MODEL
DEFAULT_OPENAI_MODEL = _DEFAULT_OPENAI_MODEL


def resolve_api_key_model(provider: str) -> str:
    """Return the direct-provider model id for ``anthropic`` or ``openai``."""
    prov = (provider or "anthropic").strip().lower()
    if prov == "openai":
        model = os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip()
        fallback = DEFAULT_OPENAI_MODEL
    else:
        prov = "anthropic"
        model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL).strip()
        fallback = DEFAULT_ANTHROPIC_MODEL
    if not model:
        model = os.environ.get("LLM_MODEL", fallback).strip()
    return model or fallback


def resolve_ingestion_job_llm() -> tuple[str, str]:
    """Return ``(llm_provider, llm_model)`` for creating an ``IngestionJob``.

    Registry mode: ``llm_model`` is a ``MODEL_REGISTRY`` key (from ``LLM_MODEL``).
    API-key mode: provider from ``LLM_PROVIDER``; model from ``ANTHROPIC_MODEL``
    or ``OPENAI_MODEL`` (falls back to ``LLM_MODEL``).
    """
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    if provider not in {"openai", "anthropic"}:
        provider = "anthropic"

    if is_registry_backend():
        from .registry import global_registry_model_name, load_model_registry

        model = global_registry_model_name()
        if not model:
            registry = load_model_registry()
            if registry:
                model = next(iter(registry))
        return provider, model or ""

    if provider == "openai":
        return provider, resolve_api_key_model("openai")
    return "anthropic", resolve_api_key_model("anthropic")


def apply_job_llm_env(*, llm_provider: str, llm_model: str) -> None:
    """Push per-job LLM fields into ``os.environ`` without breaking registry mode.

    In ``registry`` mode, ``IngestionJob.llm_model`` defaults to a direct
    provider name (``claude-sonnet-…``). Only override ``LLM_MODEL`` when the
    job value is a key in ``MODEL_REGISTRY``; otherwise keep ``.env`` routing
    (``LLM_MODEL`` + ``AGENT_MODEL_MAP``).
    """
    if is_registry_backend():
        from .registry import load_model_registry

        registry = load_model_registry()
        if llm_model and llm_model in registry:
            os.environ["LLM_MODEL"] = llm_model
        return

    os.environ["LLM_PROVIDER"] = llm_provider
    os.environ["LLM_MODEL"] = llm_model
    if llm_provider == "anthropic":
        os.environ["ANTHROPIC_MODEL"] = llm_model
    elif llm_provider == "openai":
        os.environ["OPENAI_MODEL"] = llm_model
