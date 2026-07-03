"""OAuth2 client-credentials token for UHG AI gateway (Azure AD / APIM)."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

OAUTH_ENV_VARS = (
    "AUTH_URL",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "SCOPE",
)


@dataclass(frozen=True)
class OAuthConfig:
    auth_url: str
    client_id: str
    client_secret: str
    scope: str


_token_lock = threading.Lock()
_cached_token: str = ""
_token_expires_at: float = 0.0


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def oauth_configured() -> bool:
    """True when client-credentials OAuth env vars are all set."""
    return all(_env(name) for name in OAUTH_ENV_VARS)


def load_oauth_config() -> OAuthConfig | None:
    if not oauth_configured():
        return None
    return OAuthConfig(
        auth_url=_env("AUTH_URL"),
        client_id=_env("CLIENT_ID"),
        client_secret=_env("CLIENT_SECRET"),
        scope=_env("SCOPE"),
    )


def clear_oauth_token_cache() -> None:
    """Clear cached bearer token (for tests)."""
    global _cached_token, _token_expires_at
    with _token_lock:
        _cached_token = ""
        _token_expires_at = 0.0


def fetch_oauth_token(*, force_refresh: bool = False) -> str:
    """Return a cached OAuth bearer token, refreshing when expired."""
    global _cached_token, _token_expires_at

    now = time.time()
    with _token_lock:
        if not force_refresh and _cached_token and now < _token_expires_at:
            return _cached_token

    cfg = load_oauth_config()
    if cfg is None:
        raise RuntimeError(
            "OAuth not configured. Set AUTH_URL, CLIENT_ID, CLIENT_SECRET, and SCOPE."
        )

    resp = httpx.post(
        cfg.auth_url,
        data={
            "grant_type": "client_credentials",
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "scope": cfg.scope,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"OAuth token request failed ({resp.status_code}): {resp.text[:500]}"
        )

    payload: dict[str, Any] = resp.json()
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("OAuth token response missing access_token")

    expires_in = int(payload.get("expires_in") or 3600)
    # Refresh one minute before expiry.
    _cached_token = token
    _token_expires_at = now + max(expires_in - 60, 30)
    return _cached_token
