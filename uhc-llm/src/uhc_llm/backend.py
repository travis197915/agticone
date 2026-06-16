"""Select direct API-key clients vs MODEL_REGISTRY gateway routing."""
from __future__ import annotations

import os


def get_llm_backend() -> str:
    """Return ``api_key`` or ``registry``.

    Resolution order:
    1. Explicit ``LLM_BACKEND=api_key|registry``
    2. If ``MODEL_REGISTRY`` is non-empty → ``registry``
    3. Default → ``api_key``

    Registry mode (typical app-wide setup)::

        LLM_BACKEND=registry
        MODEL_REGISTRY=uhg-gateway
        LLM_MODEL=gpt-5-mini

    ``MODEL_REGISTRY`` selects the gateway profile (JSON under
    ``config/registries/``). ``LLM_MODEL`` picks one model key for **every**
    agent — no ``AGENT_MODEL_MAP`` required. Per-agent maps remain optional.
    """
    explicit = os.environ.get("LLM_BACKEND", "").strip().lower()
    if explicit in {"api_key", "registry"}:
        return explicit
    if os.environ.get("MODEL_REGISTRY", "").strip():
        return "registry"
    return "api_key"


def is_registry_backend() -> bool:
    return get_llm_backend() == "registry"
