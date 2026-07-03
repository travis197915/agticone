"""Structured logging for tool invocations during claim processing."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

from .llm import _current_execution_run_id

logger = logging.getLogger(__name__)

_MAX_ARGS_CHARS = 240


def _slow_threshold_ms(timeout_s: int | float | None) -> int:
    """Warn when a call consumes most of its timeout budget."""
    if timeout_s:
        return int(float(timeout_s) * 1000 * 0.8)
    try:
        return int(os.environ.get("RULE_ENGINE_TOOL_SLOW_MS", "25000"))
    except ValueError:
        return 25000


def classify_tool_error(exc: BaseException | None, *, error_text: str = "") -> str:
    """Map an exception or error string to a stable kind for log grep."""
    if exc is not None:
        if isinstance(exc, requests.exceptions.Timeout):
            return "timeout"
        if isinstance(exc, requests.exceptions.SSLError):
            return "ssl"
        if isinstance(exc, requests.exceptions.ConnectionError):
            return "connection"
        if isinstance(exc, requests.exceptions.HTTPError):
            return "http"
        if isinstance(exc, TimeoutError):
            return "timeout"
    text = (error_text or str(exc or "")).lower()
    if "timeout" in text or "timed out" in text or "exceeded" in text and "s" in text:
        return "timeout"
    if "connection" in text:
        return "connection"
    if "ssl" in text:
        return "ssl"
    return "other"


def _summarize_args(args: dict[str, Any]) -> str:
    try:
        text = json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:
        text = repr(args)
    if len(text) > _MAX_ARGS_CHARS:
        return text[:_MAX_ARGS_CHARS] + "…"
    return text


def log_tool_call(
    *,
    tool_name: str,
    phase: str,
    ok: bool,
    duration_ms: int,
    route: str,
    binding_id: str = "",
    claim_id: str = "",
    error: str = "",
    args: dict[str, Any] | None = None,
    timeout_s: int | float | None = None,
    error_kind: str = "",
    attempts: int = 1,
) -> None:
    """Emit one structured log line per tool invocation.

    Timeouts and slow calls (≥80% of ``timeout_s``) log at WARNING so they are
    easy to grep in ``logs/execution_engine.log``::

        grep -E 'tool_call.*timeout|TIMEOUT|error_kind=timeout' logs/execution_engine.log
    """
    run_id = _current_execution_run_id.get()
    phase_label = phase or "-"
    kind = error_kind or (classify_tool_error(None, error_text=error) if error else "")
    slow = duration_ms >= _slow_threshold_ms(timeout_s)
    timeout_label = f" timeout_s={timeout_s}" if timeout_s else ""
    attempt_label = f" attempts={attempts}" if attempts > 1 else ""
    kind_label = f" error_kind={kind}" if kind else ""

    message = (
        f"tool_call [{phase_label}] {tool_name} route={route or '-'} "
        f"claim={claim_id or '-'} binding={binding_id or '-'} ok={ok} "
        f"ms={duration_ms}{timeout_label}{attempt_label}{kind_label} "
        f"run={run_id or '-'} args={_summarize_args(args or {})}"
        f"{f' err={error}' if error else ''}"
    )

    if (not ok and kind == "timeout") or slow:
        if slow and ok:
            message = f"SLOW {message}"
        logger.warning(message)
        return
    logger.info(message)
