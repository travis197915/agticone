"""
Loader for ``agent_tools/.env.tools``.

This file holds every upstream URL the 18 LangChain tools call.  In the
dev/mocked configuration every URL points back at the same Django process
under ``/api/mocks/...``; flipping to real upstreams is purely an
.env-level change.

We intentionally keep this loader tiny so it is safe to call from
``AppConfig.ready()`` (no Django ORM imports, no settings reads).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

try:
    from dotenv import load_dotenv as _load_dotenv
except Exception:  # pragma: no cover - dotenv is in requirements
    _load_dotenv = None  # type: ignore


HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env.tools"


def load_env_tools(path: Path | str | None = None) -> bool:
    """Populate os.environ from .env.tools.  Returns True if loaded."""
    target = Path(path) if path else ENV_FILE
    if not target.exists():
        return False
    if _load_dotenv is None:
        # Minimal fallback parser — only used when python-dotenv is missing.
        for raw in target.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return True
    _load_dotenv(target, override=False)
    return True


def get(name: str, default: str | None = None) -> str | None:
    """Read an env var (already loaded by :func:`load_env_tools`)."""
    return os.environ.get(name, default)


def get_required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"agent_tools: missing required env var '{name}'. "
            f"Add it to {ENV_FILE} or export it before starting Django."
        )
    return val


def get_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_csv(name: str, default: Iterable[str] = ()) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [s.strip() for s in raw.split(",") if s.strip()]
