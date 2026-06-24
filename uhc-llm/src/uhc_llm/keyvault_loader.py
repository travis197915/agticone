"""Azure Key Vault → os.environ loader for LLM / gateway secrets.

Reads Key Vault connection credentials from ``.env.stg`` (or env vars) and
injects secrets into ``os.environ`` so ``uhc_llm`` registry + OAuth code works
without a separate secrets file.

By default, only a focused LLM/gateway secret set is fetched for speed.
Set ``KEYVAULT_LOAD_ALL_SECRETS=true`` to restore full-vault loading.

Key Vault secret names use **hyphens** (e.g. ``CLIENT-ID``).
This module converts them to **uppercase with underscores**
(e.g. ``CLIENT_ID``) before setting them in ``os.environ``.

Required env vars (typically from ``.env.stg``):

  AZURE_KEY_VAULT_URL   – e.g. https://uaiskeyvault.vault.azure.net/
  AZURE_CLIENT_ID       – service-principal / app-registration client ID
  AZURE_TENANT_ID       – AAD tenant ID
  AZURE_CLIENT_SECRET   – service-principal client secret

Usage (called once at startup, before registry validation)::

    from uhc_llm.keyvault_loader import bootstrap_llm_secrets
    bootstrap_llm_secrets()
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_loaded: bool = False

KV_CONNECTION_VARS = (
    "AZURE_KEY_VAULT_URL",
    "AZURE_CLIENT_ID",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_SECRET",
)

# Focused secret names for registry/gateway startup performance.
LLM_KV_SECRET_NAMES = (
    "AUTH-URL",
    "CLIENT-ID",
    "CLIENT-SECRET",
    "SCOPE",
    "PROJECT-ID",
    "MODEL-REGISTRY-JSON",
    "MODEL-REGISTRY",
    "MODEL-REGISTRY-FILE",
    "AGENT-MODEL-MAP",
    "LLM-BACKEND",
    "LLM-MODEL",
    "AI-GATEWAY-API-KEY",
    "APIM-SUBSCRIPTION-KEY",
    "OPENAI-API-KEY",
    "ANTHROPIC-API-KEY",
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.strip()
        if key and key not in seen:
            out.append(key)
            seen.add(key)
    return out


def _llm_secret_names_from_env() -> list[str]:
    """Return focused secret names (KV-style) with optional env override."""
    raw = os.getenv("KEYVAULT_LLM_SECRET_NAMES", "").strip()
    if raw:
        configured = _dedupe([part for part in raw.split(",")])
    else:
        configured = list(LLM_KV_SECRET_NAMES)

    names: list[str] = []
    for name in configured:
        n = name.strip()
        if not n:
            continue
        names.append(n)
        # Accept env-style keys in override by probing KV-style equivalent too.
        if "_" in n and "-" not in n:
            names.append(n.replace("_", "-"))
    return _dedupe(names)


def keyvault_configured() -> bool:
    """True when all four Key Vault connection env vars are set."""
    return all(os.getenv(name, "").strip() for name in KV_CONNECTION_VARS)


def _repo_root() -> Path:
    """Best-effort repo root (walk up from this package)."""
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "sop_backend").is_dir() and (parent / "uhc-llm").is_dir():
            return parent
        if (parent / "manage.py").is_file():
            return parent
    return here.parents[3]


def _load_keyvault_env() -> None:
    """Load ``.env.stg`` (or ``ENV_PATH``) so KV connection vars are available."""
    explicit = os.getenv("ENV_PATH", "").strip()
    if explicit:
        load_dotenv(explicit, override=False)
        return

    root = _repo_root()
    for name in (".env.stg", ".env"):
        candidate = root / name
        if candidate.is_file():
            load_dotenv(str(candidate), override=False)
            if name == ".env.stg":
                return


def kv_name_to_env_key(kv_name: str) -> str:
    """Convert a Key Vault secret name to an env-var key.

    ``CLIENT-ID``  →  ``CLIENT_ID``
    ``MODEL-REGISTRY-JSON`` → ``MODEL_REGISTRY_JSON``
    """
    return kv_name.replace("-", "_").upper()


def _build_client() -> Any:
    """Build an authenticated ``SecretClient`` from env vars."""
    try:
        from azure.identity import ClientSecretCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError as exc:
        raise RuntimeError(
            "Azure Key Vault support requires: "
            "pip install 'uhc-llm[keyvault]'"
        ) from exc

    vault_url = os.getenv("AZURE_KEY_VAULT_URL", "").strip()
    client_id = os.getenv("AZURE_CLIENT_ID", "").strip()
    tenant_id = os.getenv("AZURE_TENANT_ID", "").strip()
    client_secret = os.getenv("AZURE_CLIENT_SECRET", "").strip()

    missing = [
        name
        for name, val in [
            ("AZURE_KEY_VAULT_URL", vault_url),
            ("AZURE_CLIENT_ID", client_id),
            ("AZURE_TENANT_ID", tenant_id),
            ("AZURE_CLIENT_SECRET", client_secret),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Cannot connect to Key Vault – missing env var(s): {', '.join(missing)}. "
            "Ensure .env.stg contains AZURE_KEY_VAULT_URL, AZURE_CLIENT_ID, "
            "AZURE_TENANT_ID, and AZURE_CLIENT_SECRET."
        )

    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        connection_timeout=10,
    )
    return SecretClient(
        vault_url=vault_url,
        credential=credential,
        connection_timeout=10,
    )


def fetch_all_secrets(client: Any) -> dict[str, str]:
    """Fetch every enabled secret from the vault (name → value)."""
    from azure.core.exceptions import ResourceNotFoundError

    secrets: dict[str, str] = {}
    for prop in client.list_properties_of_secrets():
        if not prop.enabled:
            continue
        name = prop.name
        try:
            value = client.get_secret(name).value
            if value is not None:
                secrets[name] = value
        except ResourceNotFoundError:
            logger.warning("Secret %r listed but not retrievable (skipped)", name)
    return secrets


def fetch_selected_secrets(client: Any, names: list[str]) -> dict[str, str]:
    """Fetch only selected secret names (name → value), skipping missing ones."""
    from azure.core.exceptions import ResourceNotFoundError

    secrets: dict[str, str] = {}
    for name in names:
        try:
            value = client.get_secret(name).value
            if value is not None:
                secrets[name] = value
        except ResourceNotFoundError:
            # Some environments have mixed naming / optional secrets.
            continue
    return secrets


def load_secrets_into_env(*, force: bool = False, scope: str = "all") -> dict[str, str]:
    """Fetch Key Vault secrets and inject them into ``os.environ``.

    Does not overwrite env vars that are already set (OS / .env take precedence).

    Returns the mapping of **newly injected** env-var keys → values.
    """
    global _loaded  # noqa: PLW0603
    if _loaded and not force:
        return {}

    _load_keyvault_env()
    if not keyvault_configured():
        logger.debug("Key Vault not configured; skipping secret load")
        return {}

    client = _build_client()
    if scope == "llm":
        raw_secrets = fetch_selected_secrets(client, _llm_secret_names_from_env())
    else:
        raw_secrets = fetch_all_secrets(client)

    injected: dict[str, str] = {}
    for kv_name, value in raw_secrets.items():
        env_key = kv_name_to_env_key(kv_name)
        if env_key not in os.environ:
            os.environ[env_key] = value
            injected[env_key] = value

    _loaded = True
    logger.info(
        "Loaded %d secrets from Key Vault into os.environ (%d new, scope=%s)",
        len(raw_secrets),
        len(injected),
        scope,
    )
    return injected


def bootstrap_llm_secrets(*, force: bool = False) -> dict[str, str]:
    """Load Key Vault secrets (when configured) and refresh LLM caches.

    Mirrors the reference ``ask_llm.py`` startup:

    1. ``load_secrets_into_env()`` — AUTH_URL, CLIENT_ID, MODEL_REGISTRY_JSON, …
    2. ``refresh_model_registry()`` — clear cached registry + OAuth token
    """
    scope = "all" if _env_bool("KEYVAULT_LOAD_ALL_SECRETS", default=False) else "llm"
    injected = load_secrets_into_env(force=force, scope=scope)
    from .registry import refresh_model_registry

    registry = refresh_model_registry()
    if registry:
        logger.info(
            "LLM registry refreshed after Key Vault bootstrap (%d models)",
            len(registry),
        )
    return injected
