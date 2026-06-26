"""Shared httpx transport for registry ``bedrock_claude`` invoke calls."""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .routes import BEDROCK_ANTHROPIC_VERSION

log = logging.getLogger(__name__)


def build_bedrock_body(
    *,
    messages: list[dict[str, Any]],
    max_tokens: int,
    system: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "anthropic_version": BEDROCK_ANTHROPIC_VERSION,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    if system:
        body["system"] = system
    return body


def parse_bedrock_response(data: dict[str, Any]) -> tuple[str, int, int]:
    """Normalize Bedrock / gateway JSON into ``(text, input_tokens, output_tokens)``."""
    if "choices" in data:
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            return str(content), 0, 0

    parts: list[str] = []
    for block in data.get("content") or []:
        if isinstance(block, dict):
            text = block.get("text")
            if text:
                parts.append(str(text))

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    return "".join(parts), inp, out


def post_json_with_retries(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
    max_retries: int = 4,
    base_delay: float = 1.0,
    log_context: str = "",
) -> dict[str, Any]:
    """POST JSON to a bedrock invoke URL with 429/5xx backoff."""
    last_exc: Exception | None = None
    http_resp: httpx.Response | None = None

    for attempt in range(max_retries + 1):
        with httpx.Client(timeout=timeout) as client:
            http_resp = client.post(url, headers=headers, json=body)

        if http_resp.status_code == 200:
            return http_resp.json()

        retryable = http_resp.status_code == 429 or http_resp.status_code >= 500
        log.warning(
            "bedrock_http%s status=%d url=%s attempt=%d/%d retryable=%s body=%s",
            f" {log_context}" if log_context else "",
            http_resp.status_code,
            url,
            attempt + 1,
            max_retries + 1,
            retryable,
            http_resp.text[:500],
        )
        if retryable and attempt < max_retries:
            delay = base_delay * (2 ** attempt)
            retry_after = http_resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
            time.sleep(delay)
            continue

        last_exc = httpx.HTTPStatusError(
            message=f"Client error '{http_resp.status_code}' for url '{url}'",
            request=http_resp.request,
            response=http_resp,
        )
        break

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Bedrock invoke failed without a response for {url}")
