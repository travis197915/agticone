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
