"""Dual-provider LLM helper.

Adapted from `uhc_sop_ingestion.agents.a07_enrich._llm_call`. The full
guardrail chain (schema-validate → retry on primary → cross-provider fallback)
is kept; each call attempt is also persisted to ``sop_ingestion.LLMCallLog``
with ``execution_run`` set to the current ``RuleExecutionRun`` (when one is
in scope) instead of an ``IngestionJob``.

The current ``execution_run_id`` is carried through a ContextVar set by
``RuleEnginePipeline.run(...)`` for the duration of one claim, so the deeply
nested LLM calls don't need it threaded through every signature.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any

from .config import EngineConfig

logger = logging.getLogger(__name__)

# Global cap on concurrent provider invocations. The engine now evaluates SOP
# cursors AND sibling rules concurrently, so without a ceiling we could fire
# dozens of simultaneous Anthropic/OpenAI requests and trip provider rate
# limits. This bounds total in-flight LLM calls across every worker thread;
# tune via RULE_ENGINE_LLM_CONCURRENCY (default 10).
try:
    _LLM_CONCURRENCY = max(1, int(os.environ.get("RULE_ENGINE_LLM_CONCURRENCY", "10")))
except ValueError:
    _LLM_CONCURRENCY = 10
_llm_semaphore = threading.BoundedSemaphore(_LLM_CONCURRENCY)

# Anthropic prompt caching. When enabled (default), a caller may pass a static
# ``system_prompt`` and/or a per-claim-invariant ``cache_prefix`` to ``llm_call``;
# on the direct-Anthropic path those blocks are marked ``cache_control:
# ephemeral`` so their tokens are billed at the cache rate (~10%) on every call
# after the first within the 5-minute TTL. This is a pure billing optimization —
# the model receives byte-identical content, so verdicts/rationale are unchanged.
# Set RULE_ENGINE_PROMPT_CACHE=0 to disable and fall back to a single flat prompt.
_PROMPT_CACHE_ENABLED = os.environ.get("RULE_ENGINE_PROMPT_CACHE", "1").strip() not in (
    "0",
    "false",
    "False",
    "no",
    "",
)


# ── Per-claim / per-batch context vars ───────────────────────────────────────
#
# ``execution_run_context`` is set by ``RuleEnginePipeline.run(...)`` once per
# claim so that the deeply nested LLM calls + the per-Shape evaluator can
# stamp the right ``RuleExecutionRun`` id on telemetry without threading it
# through every signature.
#
# ``batch_context`` is set by the streaming Celery task (``execution_app.tasks
# .run_batch_async``) for the lifetime of one batch.  When set, the engine
# publishes ``shape_start`` and ``rule_evaluated`` events to the Redis
# pub/sub channel ``batch:<batch_id>``.  When unset (single-claim runs,
# ingestion-pipeline reuse, unit tests) the publish is a no-op.

_current_execution_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "uhc_execution_engine.current_run_id", default=None
)

_current_batch_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "uhc_execution_engine.current_batch_id", default=None
)


@contextmanager
def execution_run_context(run_id: str | None):
    """Stamp every LLMCallLog row written during this block with run_id."""
    token = _current_execution_run_id.set(run_id)
    try:
        yield
    finally:
        _current_execution_run_id.reset(token)


@contextmanager
def batch_context(batch_id: str | None):
    """Route ``publish_event`` calls during this block to ``batch:<batch_id>``."""
    token = _current_batch_id.set(batch_id)
    try:
        yield
    finally:
        _current_batch_id.reset(token)


# ── SSE side-channel publisher ───────────────────────────────────────────────

_redis_client: Any = None  # lazy module-scope cache


def _get_redis():
    """Return a cached redis.Redis client built from ``REDIS_URL``.

    Imported lazily so unit tests that never publish don't have to install
    the redis package or set REDIS_URL.
    """
    global _redis_client
    if _redis_client is None:
        import redis as _r  # noqa: WPS433 — lazy import is intentional

        url = os.environ.get("REDIS_URL")
        if not url:
            raise RuntimeError(
                "REDIS_URL env var not set; cannot publish SSE events. "
                "Set REDIS_URL or avoid calling publish_event when there is "
                "no batch context in scope."
            )
        _redis_client = _r.Redis.from_url(url)
    return _redis_client


def publish_event(kind: str, payload: dict[str, Any]) -> None:
    """Publish one SSE event to ``batch:<batch_id>`` on Redis.

    Short-circuits when no batch is in scope (single-claim runs, ingestion
    pipeline LLM calls, unit tests).  Best-effort — Redis failures are
    logged and swallowed.  The DB is the source of truth; the stream is a
    side channel and must never break the engine.

    ``payload`` is merged into a standard envelope::

        {"kind", "batch_id", "run_id", "ts", **payload}
    """
    batch_id = _current_batch_id.get()
    if not batch_id:
        return
    envelope = {
        "kind": kind,
        "batch_id": batch_id,
        "run_id": _current_execution_run_id.get() or "",
        "ts": time.time(),
        **payload,
    }
    try:
        _get_redis().publish(f"batch:{batch_id}", json.dumps(envelope, default=str))
    except Exception as exc:  # pragma: no cover — telemetry must not abort
        logger.warning("publish_event %s failed: %s", kind, exc)


# ── Provider helpers ─────────────────────────────────────────────────────────


def _make_anthropic_llm(cfg: EngineConfig, max_tokens: int):
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=cfg.anthropic_model,
        api_key=cfg.anthropic_api_key,
        max_tokens=max_tokens,
    )


def _make_openai_llm(cfg: EngineConfig, max_tokens: int):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=cfg.openai_model,
        api_key=cfg.openai_api_key,
        max_tokens=max_tokens,
        model_kwargs={"response_format": {"type": "json_object"}},
    )


def _strip_markdown(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()
    return text


def _loads_first_json(text: str) -> Any:
    """Parse the FIRST complete JSON value in ``text``, ignoring trailing data.

    Some gateway models (notably Bedrock Claude/opus via the UHG gateway) emit a
    valid JSON object followed by extra content — a second object, a repeated
    answer, or trailing prose/notes. ``json.loads`` rejects that with
    "Extra data: line N column 1", failing an otherwise-usable response. We seek
    to the first ``{``/``[`` and use ``raw_decode`` to take just the leading JSON
    value.
    """
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _end = dec.raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    # Nothing salvageable — re-raise the original strict error for the caller.
    return json.loads(text)


def _parse_json(text: str) -> Any:
    cleaned = _strip_markdown(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Tolerate leading prose / trailing "extra data" after the JSON value.
        return _loads_first_json(cleaned)


def _token_usage(resp) -> tuple[int, int]:
    usage = getattr(resp, "usage_metadata", None) or {}
    return (
        usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0),
        usage.get("output_tokens", 0) or usage.get("completion_tokens", 0),
    )


def _log_llm_call(
    *,
    agent_name: str,
    stage: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: int,
    success: bool,
    error: str = "",
) -> None:
    """Persist one LLMCallLog row, stamped with the current execution_run_id.

    Best-effort: telemetry failures never break the pipeline. The console
    log line is kept so calls are still observable when the DB write fails
    or no run context is set (e.g. ad-hoc imports during tests).
    """
    run_id = _current_execution_run_id.get()
    logger.info(
        "llm_call %s/%s [%s] %s tok=%s+%s ms=%s success=%s run=%s%s",
        provider,
        model,
        stage,
        agent_name,
        prompt_tokens,
        completion_tokens,
        duration_ms,
        success,
        run_id or "-",
        f" err={error}" if error else "",
    )
    if not run_id:
        # No run in scope (e.g. unit tests with llm.py imported standalone).
        # Skip persistence rather than write an orphan row.
        return
    try:
        from sop_ingestion.models import LLMCallLog

        LLMCallLog.objects.create(
            job=None,
            execution_run_id=run_id,
            stage=stage,
            agent_name=agent_name,
            llm_provider=provider,
            llm_model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            duration_ms=duration_ms,
            success=success,
            error_message=error or "",
        )
    except Exception as exc:  # pragma: no cover — telemetry never breaks the run
        logger.warning("LLMCallLog write failed (run=%s): %s", run_id, exc)


def _validate(
    data: Any, expected_type: type, required_keys: list[str] | None = None
) -> bool:
    if not isinstance(data, expected_type):
        return False
    if required_keys:
        sample = data[0] if isinstance(data, list) and data else data
        if isinstance(sample, dict) and not all(k in sample for k in required_keys):
            return False
    return True


# ── Core dispatcher ──────────────────────────────────────────────────────────


def _parse_llm_payload(
    content: str,
    *,
    expected_type: type,
    required_keys: list[str] | None,
) -> Any:
    data = _parse_json(content)
    if expected_type is list and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
    # The model occasionally wraps the object in an outer key
    # (e.g. {"result": {...}} / {"evaluation": {...}}). If the required keys
    # are not present at the top level, unwrap the first nested dict that
    # carries them — mirrors the list-unwrap above so a well-formed answer
    # doesn't burn a retry on a schema mismatch.
    if (
        expected_type is dict
        and required_keys
        and isinstance(data, dict)
        and not all(k in data for k in required_keys)
    ):
        for v in data.values():
            if isinstance(v, dict) and all(k in v for k in required_keys):
                data = v
                break
    if not _validate(data, expected_type, required_keys):
        raise ValueError(
            f"Schema mismatch: expected {expected_type.__name__}"
            f" with keys {required_keys}, got {type(data).__name__}"
        )
    return data


def llm_call(
    cfg: EngineConfig,
    prompt: str,
    *,
    agent_name: str,
    stage: str,
    fallback: Any,
    provider: str = "anthropic",
    expected_type: type = dict,
    required_keys: list[str] | None = None,
    max_tokens: int | None = None,
    system_prompt: str | None = None,
    cache_prefix: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Run a guarded LLM call.

    Returns ``(parsed_data, meta)`` where ``meta`` carries the provider/model
    that ultimately succeeded plus total duration; useful for `RuleEvaluation`
    persistence rows.

    ``system_prompt`` (fully static across every call) and ``cache_prefix``
    (invariant across one claim's many rule evaluations) are optional. On the
    direct-Anthropic path they are sent as ``cache_control: ephemeral`` blocks
    so their tokens bill at the cache rate on repeat calls; ``prompt`` carries
    the small per-rule tail that changes each call. Backends that take a single
    string (registry gateway, OpenAI fallback) receive the three concatenated in
    the same logical order, so behavior is unchanged when caching is off/unused.
    """
    from uhc_llm import invoke_prompt, is_registry_backend

    max_tokens = max_tokens or cfg.llm_max_tokens
    meta: dict[str, Any] = {"provider": "", "model": "", "ms": 0, "attempts": 0}

    # Flat single-string form for backends that don't support cache blocks.
    flat_prompt = "\n\n".join(p for p in (system_prompt, cache_prefix, prompt) if p)

    if is_registry_backend():
        from uhc_llm.registry import load_agent_model_map, resolve_registry_model_name

        used_prompt = flat_prompt + (
            "\n\nIMPORTANT: Reply with valid JSON only. No markdown, no explanation."
        )
        if expected_type is list:
            used_prompt += '\n\nWrap the array in a JSON object: {"items": [...]}'

        def _attempt_registry(
            current_prompt: str, label: str, *, model_name: str | None = None
        ):
            t0 = time.time()
            meta["attempts"] += 1
            endpoint_hint = "unknown endpoint"
            try:
                from uhc_llm.gateway import describe_registry_target
                from uhc_llm.registry import get_model_spec, resolve_registry_model_name

                resolved = model_name or resolve_registry_model_name(agent_name)
                endpoint_hint = describe_registry_target(
                    get_model_spec(resolved),
                    json_mode=(expected_type in (dict, list)),
                )
            except Exception:
                pass
            try:
                resp = invoke_prompt(
                    agent_name=agent_name,
                    prompt=current_prompt,
                    max_tokens=max_tokens,
                    json_mode=(expected_type in (dict, list)),
                    cfg=cfg,
                    model_name=model_name,
                )
                ms = int((time.time() - t0) * 1000)
                _log_llm_call(
                    agent_name=f"{agent_name}[{label}]",
                    stage=stage,
                    provider=resp.provider,
                    model=resp.model,
                    prompt_tokens=resp.prompt_tokens,
                    completion_tokens=resp.completion_tokens,
                    duration_ms=ms,
                    success=True,
                )
                data = _parse_llm_payload(
                    resp.content,
                    expected_type=expected_type,
                    required_keys=required_keys,
                )
                meta.update(provider=resp.provider, model=resp.model, ms=ms)
                return data, None
            except Exception as exc:
                ms = int((time.time() - t0) * 1000)
                logger.warning(
                    "llm_call [%s/%s] -> %s: %s",
                    agent_name,
                    label,
                    endpoint_hint,
                    exc,
                )
                _log_llm_call(
                    agent_name=f"{agent_name}[{label}]",
                    stage=stage,
                    provider="registry",
                    model=model_name or "auto",
                    prompt_tokens=0,
                    completion_tokens=0,
                    duration_ms=ms,
                    success=False,
                    error=f"{endpoint_hint}: {exc}",
                )
                return None, str(exc)

        current_prompt = used_prompt
        last_error = ""
        for attempt in range(1, cfg.llm_retries + 1):
            if last_error:
                current_prompt = (
                    f"{used_prompt}\n\n[Previous attempt failed: {last_error}. "
                    "Fix the JSON format and try again.]"
                )
            data, err = _attempt_registry(current_prompt, f"p{attempt}")
            if data is not None:
                return data, meta
            last_error = err or "unknown error"

        default_model = load_agent_model_map().get("__default__")
        try:
            primary_model = resolve_registry_model_name(agent_name)
        except RuntimeError:
            primary_model = None
        if default_model and default_model != primary_model:
            data, _err = _attempt_registry(
                used_prompt,
                "default-fallback",
                model_name=default_model,
            )
            if data is not None:
                return data, meta

        logger.error(
            "llm_call [%s]: registry attempts failed; returning fallback", agent_name
        )
        return fallback, meta

    from langchain_core.messages import HumanMessage, SystemMessage

    alt_provider = "openai" if provider == "anthropic" else "anthropic"
    primary_fn = _make_anthropic_llm if provider == "anthropic" else _make_openai_llm
    fallback_fn = _make_openai_llm if alt_provider == "openai" else _make_anthropic_llm
    primary_model = cfg.anthropic_model if provider == "anthropic" else cfg.openai_model
    fallback_model = (
        cfg.openai_model if alt_provider == "openai" else cfg.anthropic_model
    )

    # ``prompt`` here is the per-rule *tail* (the only part that changes each
    # call). The JSON-format reminder must stay at the very end, after the tail.
    if provider == "anthropic":
        prompt = (
            prompt
            + "\n\nIMPORTANT: Reply with valid JSON only. No markdown, no explanation."
        )
    elif provider == "openai" and expected_type is list:
        prompt = prompt + '\n\nWrap the array in a JSON object: {"items": [...]}'

    def _messages_for(prov: str, tail: str):
        """Build the message list for one attempt.

        On the Anthropic primary path (when caching is enabled and a static
        system prompt / per-claim prefix was supplied) the invariant blocks are
        emitted as ``cache_control: ephemeral`` so they bill at the cache rate.
        Every other path concatenates the pieces into plain text in the same
        logical order — identical content, no behavioral change.
        """
        cache_this = (
            _PROMPT_CACHE_ENABLED
            and prov == "anthropic"
            and bool(system_prompt or cache_prefix)
        )
        if cache_this:
            msgs: list[Any] = []
            if system_prompt:
                msgs.append(
                    SystemMessage(
                        content=[
                            {
                                "type": "text",
                                "text": system_prompt,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ]
                    )
                )
            human_blocks: list[dict[str, Any]] = []
            if cache_prefix:
                human_blocks.append(
                    {
                        "type": "text",
                        "text": cache_prefix,
                        "cache_control": {"type": "ephemeral"},
                    }
                )
            human_blocks.append({"type": "text", "text": tail})
            msgs.append(HumanMessage(content=human_blocks))
            return msgs
        # Flat path: keep system separate but send everything as plain text.
        msgs = []
        if system_prompt:
            msgs.append(SystemMessage(content=system_prompt))
        human = "\n\n".join(p for p in (cache_prefix, tail) if p)
        msgs.append(HumanMessage(content=human))
        return msgs

    def _attempt(make_fn, prov, model, tail, label):
        t0 = time.time()
        meta["attempts"] += 1
        try:
            llm = make_fn(cfg, max_tokens)
            with _llm_semaphore:
                resp = llm.invoke(_messages_for(prov, tail))
            inp, out = _token_usage(resp)
            ms = int((time.time() - t0) * 1000)
            _log_llm_call(
                agent_name=f"{agent_name}[{label}]",
                stage=stage,
                provider=prov,
                model=model,
                prompt_tokens=inp,
                completion_tokens=out,
                duration_ms=ms,
                success=True,
            )
            data = _parse_llm_payload(
                resp.content if isinstance(resp.content, str) else str(resp.content),
                expected_type=expected_type,
                required_keys=required_keys,
            )
            meta.update(provider=prov, model=model, ms=ms)
            return data, None
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            logger.warning("llm_call [%s/%s]: %s", agent_name, label, exc)
            _log_llm_call(
                agent_name=f"{agent_name}[{label}]",
                stage=stage,
                provider=prov,
                model=model,
                prompt_tokens=0,
                completion_tokens=0,
                duration_ms=ms,
                success=False,
                error=str(exc),
            )
            return None, str(exc)

    last_error = ""
    current_tail = prompt
    for attempt in range(1, cfg.llm_retries + 1):
        if last_error:
            current_tail = (
                f"{prompt}\n\n[Previous attempt failed: {last_error}. "
                "Fix the JSON format and try again.]"
            )
        data, err = _attempt(
            primary_fn, provider, primary_model, current_tail, f"p{attempt}"
        )
        if data is not None:
            return data, meta
        last_error = err or "unknown error"

    fb_tail = prompt
    if alt_provider == "anthropic":
        fb_tail = (
            prompt
            + "\n\nIMPORTANT: Reply with valid JSON only. No markdown, no explanation."
        )
    data, _err = _attempt(
        fallback_fn, alt_provider, fallback_model, fb_tail, "fallback"
    )
    if data is not None:
        return data, meta

    logger.error("llm_call [%s]: all attempts failed; returning fallback", agent_name)
    return fallback, meta
