"""
Tiny requests-based HTTP helper with retry/backoff.

Centralises:

* Default User-Agent / Accept headers.
* 3-retry exponential backoff on transient errors (timeouts, 5xx, 429).
* SSL verify defaults from ``SSL_VERIFY`` env (defaults to True).
* Optional in-process JSON-body cache shared across modules.

Tools that need OAuth or per-host headers compose on top of this helper.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Mapping

import requests

LOGGER = logging.getLogger("agent_tools.http")

_DEFAULT_TIMEOUT = float(os.environ.get("AGENT_TOOLS_HTTP_TIMEOUT", "30"))
_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


def _verify_ssl() -> bool:
    return os.environ.get("SSL_VERIFY", "true").strip().lower() not in {"0", "false", "no"}


def request_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json: Any = None,
    data: Any = None,
    timeout: float | None = None,
    retries: int = 3,
    backoff: float = 0.5,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Make an HTTP call and return ``(status_code, body, response_headers)``.

    ``body`` is the parsed JSON if the response is JSON, otherwise a dict
    with ``{"raw": <text>}`` so callers always get a dict back.
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=dict(headers or {}),
                params=dict(params) if params else None,
                json=json,
                data=data,
                timeout=timeout or _DEFAULT_TIMEOUT,
                verify=_verify_ssl(),
            )
            if resp.status_code in _TRANSIENT_STATUSES and attempt < retries:
                LOGGER.info(
                    "transient HTTP %s on %s (attempt %s/%s); backing off",
                    resp.status_code, url, attempt, retries,
                )
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype or resp.text.strip().startswith(("{", "[")):
                try:
                    body = resp.json()
                except ValueError:
                    body = {"raw": resp.text}
            else:
                body = {"raw": resp.text}
            if not isinstance(body, dict):
                # JSON array etc — wrap so callers get a stable shape.
                body = {"items": body}
            return resp.status_code, body, dict(resp.headers)
        except (requests.RequestException, OSError) as exc:
            last_exc = exc
            if attempt >= retries:
                break
            LOGGER.warning(
                "HTTP exception on %s (attempt %s/%s): %s",
                url, attempt, retries, exc,
            )
            time.sleep(backoff * (2 ** (attempt - 1)))
    # All retries exhausted.
    raise RuntimeError(
        f"HTTP call to {url} failed after {retries} attempts: {last_exc}"
    )
