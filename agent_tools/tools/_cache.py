"""
In-process TTL cache shim replacing thynkr_bhagenticai.tool_cache.ToolCache.

Surface compatibility:
    cache = ToolCache(name="...")  # name argument ignored, accepted for parity
    cache.get(ns, key) -> value | None
    cache.set(ns, key, value, ttl=None)
"""
from __future__ import annotations

import threading
import time
from typing import Any


class ToolCache:
    """Thread-safe in-process cache with optional per-key TTL."""

    _STORE: dict[tuple[str, str], tuple[Any, float | None]] = {}
    _LOCK = threading.Lock()

    def __init__(self, name: str | None = None, default_ttl: float | None = None) -> None:
        self._name = name or "default"
        self._default_ttl = default_ttl

    def get(self, ns: str, key: str) -> Any:
        full_ns = f"{self._name}:{ns}"
        with self._LOCK:
            entry = self._STORE.get((full_ns, key))
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.time() > expires_at:
            with self._LOCK:
                self._STORE.pop((full_ns, key), None)
            return None
        return value

    def set(self, ns: str, key: str, value: Any, ttl: float | None = None) -> None:
        ttl = ttl if ttl is not None else self._default_ttl
        expires_at = time.time() + ttl if ttl else None
        full_ns = f"{self._name}:{ns}"
        with self._LOCK:
            self._STORE[(full_ns, key)] = (value, expires_at)

    @classmethod
    def clear(cls) -> None:
        with cls._LOCK:
            cls._STORE.clear()
