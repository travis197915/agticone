"""Helpers for long-running workers that use Django ORM intermittently."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from django.db import close_old_connections
from django.db.utils import InterfaceError, OperationalError

log = logging.getLogger(__name__)

_RETRYABLE_DB_ERRORS = (OperationalError, InterfaceError)

T = TypeVar("T")


def ensure_db_connection() -> None:
    """Drop stale connections before ORM use (remote PG idle timeouts)."""
    close_old_connections()


def call_with_db_retry(
    fn: Callable[..., T],
    /,
    *args,
    attempts: int = 3,
    backoff_s: float = 0.5,
    **kwargs,
) -> T:
    """Run ``fn``; on dropped PG connections, refresh and retry."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        ensure_db_connection()
        try:
            return fn(*args, **kwargs)
        except _RETRYABLE_DB_ERRORS as exc:
            last_exc = exc
            if attempt >= attempts - 1:
                raise
            log.warning(
                "DB connection error in %s (attempt %s/%s): %s",
                getattr(fn, "__name__", fn),
                attempt + 1,
                attempts,
                exc,
            )
            time.sleep(backoff_s * (attempt + 1))
    assert last_exc is not None
    raise last_exc
