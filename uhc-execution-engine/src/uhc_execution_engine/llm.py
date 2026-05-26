"""Dual-provider LLM helper.

Adapted from `uhc_sop_ingestion.agents.a07_enrich._llm_call`. The full
guardrail chain (schema-validate → retry on primary → cross-provider fallback)
is kept; we drop the `PipelineLogger` dependency and log directly to the
`LLMCallLog` Django model so we don't carry the ingestion-pipeline config
object around.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .config import EngineConfig

logger = logging.getLogger(__name__)


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


def _parse_json(text: str) -> Any:
    return json.loads(_strip_markdown(text))


def _token_usage(resp) -> tuple[int, int]:
    usage = getattr(resp, "usage_metadata", None) or {}
    return (
        usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0),
        usage.get("output_tokens", 0) or usage.get("completion_tokens", 0),
    )


def _log_llm_call(*, agent_name: str, stage: str, provider: str, model: str,
                  prompt_tokens: int, completion_tokens: int,
                  duration_ms: int, success: bool, error: str = "") -> None:
    """Per-attempt LLM telemetry.

    `sop_ingestion.LLMCallLog` requires an IngestionJob FK, which the rule
    engine doesn't have — so for now we log to the standard logger only and
    rely on per-evaluation `llm_provider` / `llm_ms` columns on `RuleEvaluation`
    for cost reporting. If we later add an engine-level run table FK on
    LLMCallLog (or a dedicated table), wire it here.
    """
    logger.info(
        "llm_call %s/%s [%s] %s tok=%s+%s ms=%s success=%s%s",
        provider, model, stage, agent_name,
        prompt_tokens, completion_tokens, duration_ms, success,
        f" err={error}" if error else "",
    )


def _validate(data: Any, expected_type: type,
              required_keys: list[str] | None = None) -> bool:
    if not isinstance(data, expected_type):
        return False
    if required_keys:
        sample = data[0] if isinstance(data, list) and data else data
        if isinstance(sample, dict) and not all(k in sample for k in required_keys):
            return False
    return True


# ── Core dispatcher ──────────────────────────────────────────────────────────


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
) -> tuple[Any, dict[str, Any]]:
    """Run a guarded LLM call.

    Returns ``(parsed_data, meta)`` where ``meta`` carries the provider/model
    that ultimately succeeded plus total duration; useful for `RuleEvaluation`
    persistence rows.
    """
    from langchain_core.messages import HumanMessage

    max_tokens = max_tokens or cfg.llm_max_tokens
    alt_provider = "openai" if provider == "anthropic" else "anthropic"
    primary_fn = _make_anthropic_llm if provider == "anthropic" else _make_openai_llm
    fallback_fn = _make_openai_llm if alt_provider == "openai" else _make_anthropic_llm
    primary_model = cfg.anthropic_model if provider == "anthropic" else cfg.openai_model
    fallback_model = cfg.openai_model if alt_provider == "openai" else cfg.anthropic_model

    if provider == "anthropic":
        prompt = prompt + "\n\nIMPORTANT: Reply with valid JSON only. No markdown, no explanation."
    elif provider == "openai" and expected_type is list:
        prompt = prompt + '\n\nWrap the array in a JSON object: {"items": [...]}'

    meta: dict[str, Any] = {"provider": "", "model": "", "ms": 0}

    def _attempt(make_fn, prov, model, used_prompt, label):
        t0 = time.time()
        try:
            llm = make_fn(cfg, max_tokens)
            resp = llm.invoke([HumanMessage(content=used_prompt)])
            inp, out = _token_usage(resp)
            ms = int((time.time() - t0) * 1000)
            _log_llm_call(agent_name=f"{agent_name}[{label}]", stage=stage,
                          provider=prov, model=model, prompt_tokens=inp,
                          completion_tokens=out, duration_ms=ms, success=True)
            data = _parse_json(resp.content)
            if expected_type is list and isinstance(data, dict):
                # OpenAI json_object mode wraps lists in an object
                for v in data.values():
                    if isinstance(v, list):
                        data = v
                        break
            if not _validate(data, expected_type, required_keys):
                raise ValueError(
                    f"Schema mismatch: expected {expected_type.__name__}"
                    f" with keys {required_keys}, got {type(data).__name__}"
                )
            meta.update(provider=prov, model=model, ms=ms)
            return data, None
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            logger.warning("llm_call [%s/%s]: %s", agent_name, label, exc)
            _log_llm_call(agent_name=f"{agent_name}[{label}]", stage=stage,
                          provider=prov, model=model, prompt_tokens=0,
                          completion_tokens=0, duration_ms=ms,
                          success=False, error=str(exc))
            return None, str(exc)

    last_error = ""
    current_prompt = prompt
    for attempt in range(1, cfg.llm_retries + 1):
        if last_error:
            current_prompt = (
                f"{prompt}\n\n[Previous attempt failed: {last_error}. "
                "Fix the JSON format and try again.]"
            )
        data, err = _attempt(primary_fn, provider, primary_model,
                             current_prompt, f"p{attempt}")
        if data is not None:
            return data, meta
        last_error = err or "unknown error"

    fb_prompt = prompt
    if alt_provider == "anthropic":
        fb_prompt = prompt + "\n\nIMPORTANT: Reply with valid JSON only. No markdown, no explanation."
    data, _err = _attempt(fallback_fn, alt_provider, fallback_model, fb_prompt, "fallback")
    if data is not None:
        return data, meta

    logger.error("llm_call [%s]: all attempts failed; returning fallback", agent_name)
    return fallback, meta
