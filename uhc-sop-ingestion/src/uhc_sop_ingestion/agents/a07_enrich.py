"""LLM ENRICHMENT LAYER — 10 agents.

DESIGN PRINCIPLES
─────────────────
• Zero regex / NLP — every insight comes from an LLM call.
• Dual-provider: OpenAI for structured extraction, Anthropic for
  reasoning/classification.  Each agent declares its preferred provider.
• Guardrails on every call:
    1. OpenAI → response_format=json_object (enforced JSON)
    2. Anthropic → explicit JSON-only instruction + markdown strip
    3. Schema check — validate expected keys/type before accepting output
    4. Retry ×2 with the primary provider, appending the error to the prompt
    5. Cross-provider fallback — if all retries fail, try the other provider
    6. Every attempt (success or failure) logged to Postgres via PipelineLogger

Provider assignment
───────────────────
OpenAI (gpt-4o family) — structured extraction tasks:
    date_condition_extractor, group_rule_extractor,
    pre_section_rule_extractor, cross_reference_resolver

Anthropic (claude-sonnet family) — reasoning / judgment tasks:
    step_question_refiner, decision_row_classifier,
    rule_semantic_enricher, ambiguous_term_resolver,
    potf_validator, summary_generator

Renamed agents (no "NLP" suffix anywhere):
    date_condition_nlp  → date_condition_extractor
    group_rule_nlp      → group_rule_extractor
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)


def _forced_provider() -> str:
    """Optional single-provider override.

    Set ``LLM_FORCE_PROVIDER=anthropic`` (or ``openai``) to route EVERY LLM
    call through that provider, ignoring each agent's preferred provider and
    skipping the cross-provider fallback. Useful when one provider's quota is
    exhausted. Empty/unset keeps the normal dual-provider behaviour.
    """
    val = os.environ.get("LLM_FORCE_PROVIDER", "").strip().lower()
    return val if val in {"anthropic", "openai"} else ""


# ── Provider helpers ──────────────────────────────────────────────────────────

def _make_openai_llm(cfg, max_tokens: int = 4096):
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=cfg.openai_model,
        api_key=cfg.openai_api_key,
        max_tokens=max_tokens,
        # Deterministic extraction: same SOP → same rules every run. Removes the
        # run-to-run row-count variance that lets a rule appear in one run and
        # vanish in another.
        temperature=0,
        model_kwargs={"response_format": {"type": "json_object"}},
    )


def _make_anthropic_llm(cfg, max_tokens: int = 4096):
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=cfg.anthropic_model,
        api_key=cfg.anthropic_api_key,
        max_tokens=max_tokens,
        temperature=0,   # deterministic extraction (see _make_openai_llm)
    )


def _strip_markdown(text: str) -> str:
    """Strip ```json ... ``` fences from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # drop first fence line and last fence line
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()
    return text


def _parse_json(text: str) -> Any:
    """Parse JSON, stripping markdown fences first."""
    return json.loads(_strip_markdown(text))


def _salvage_json_array(text: str) -> list | None:
    """Best-effort recovery of complete objects from a TRUNCATED JSON array.

    LLM responses that hit the output-token cap get cut off mid-string, so
    ``json.loads`` raises and we would otherwise lose the entire batch. This
    walks the leading ``[`` and decodes as many complete top-level objects as
    possible, discarding only the final partial one. Returns the recovered list
    (possibly empty) or ``None`` when the payload is not array-shaped.
    """
    s = _strip_markdown(text)
    start = s.find("[")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    objs: list = []
    i, n = start + 1, len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n,":
            i += 1
        if i >= n or s[i] == "]":
            break
        try:
            obj, end = decoder.raw_decode(s, i)
        except json.JSONDecodeError:
            break  # truncated / malformed from here on — keep what we have
        objs.append(obj)
        i = end
    return objs


def _salvage_json_object(text: str) -> dict | None:
    """Best-effort recovery of a dict whose array/scalar values were TRUNCATED.

    Walks the top-level object members (depth 1 only) and decodes each
    ``"key": value`` pair, salvaging array values element-by-element. Stops at
    the first member that is itself cut off — keeping everything decoded so far.
    Covers the ``{"nodes": [...], "edges": [...]}`` graph-synthesis payloads
    (a16) that hit the token cap mid-array.
    """
    s = _strip_markdown(text)
    start = s.find("{")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    out: dict = {}
    i, n = start + 1, len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n,":
            i += 1
        if i >= n or s[i] == "}":
            break
        if s[i] != '"':
            break  # not a key — malformed from here
        try:
            key, end = decoder.raw_decode(s, i)
        except json.JSONDecodeError:
            break
        i = end
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n or s[i] != ":":
            break
        i += 1
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        if s[i] == "[":
            out[key] = _salvage_json_array(s[i:]) or []
            try:                      # advance past a complete array if possible
                _, end = decoder.raw_decode(s, i)
                i = end
            except json.JSONDecodeError:
                break                 # array was the truncated tail — done
        else:
            try:
                val, end = decoder.raw_decode(s, i)
            except json.JSONDecodeError:
                break                 # scalar/object value truncated — done
            out[key] = val
            i = end
    return out or None


def _token_usage(resp) -> tuple[int, int]:
    usage = getattr(resp, "usage_metadata", None) or {}
    return (usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0),
            usage.get("output_tokens", 0) or usage.get("completion_tokens", 0))


def _unwrap_if_needed(data: Any, expected_type: type) -> Any:
    """OpenAI json_object mode always returns a dict, never a bare list.
    If we expected a list but got a dict, find the first list value inside it.
    """
    if expected_type is list and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return data


def _validate_schema(data: Any, expected_type: type,
                     required_keys: list[str] | None = None) -> bool:
    """Return True if data matches expected_type and contains required_keys."""
    if not isinstance(data, expected_type):
        return False
    if required_keys:
        sample = data[0] if isinstance(data, list) and data else data
        if isinstance(sample, dict):
            if not all(k in sample for k in required_keys):
                return False
    return True


def _parse_llm_output(
    raw_content: str,
    *,
    agent_name: str,
    attempt_label: str,
    expected_type: type,
    required_keys: list[str] | None,
) -> Any:
    """Parse, salvage, unwrap, and schema-validate one LLM text response."""
    try:
        data = _parse_json(raw_content)
    except json.JSONDecodeError as je:
        if expected_type is list:
            salvaged = _salvage_json_array(raw_content)
        elif expected_type is dict:
            salvaged = _salvage_json_object(raw_content)
            if (salvaged and required_keys
                    and any(k in salvaged for k in required_keys)
                    and all(isinstance(salvaged[k], list)
                            for k in required_keys if k in salvaged)):
                for k in required_keys:
                    salvaged.setdefault(k, [])
        else:
            salvaged = None
        if not salvaged:
            raise
        logger.warning(
            "llm_call [%s/%s]: salvaged %d objects from truncated JSON (%s)",
            agent_name, attempt_label, len(salvaged), je,
        )
        data = salvaged
    data = _unwrap_if_needed(data, expected_type)
    if not _validate_schema(data, expected_type, required_keys):
        raise ValueError(
            f"Schema mismatch: expected {expected_type.__name__} "
            f"with keys {required_keys}, got {type(data).__name__}"
        )
    return data


def _llm_call_via_registry(
    cfg,
    prompt: str,
    fallback: Any,
    agent_name: str,
    *,
    expected_type: type = dict,
    required_keys: list[str] | None = None,
    stage: str = "enrich_stage",
    max_retries: int = 2,
    max_tokens: int = 4096,
) -> Any:
    """Registry-backend variant of ``_llm_call`` (MODEL_REGISTRY + gateway)."""
    from uhc_llm import invoke_prompt
    from uhc_llm.prompts import enrich_for_json
    from uhc_llm.registry import (
        load_agent_model_map,
        load_model_registry,
        resolve_registry_model_name,
    )
    from uhc_llm.router import describe_target

    pg_logger = getattr(cfg, "_pg_logger", None)
    json_mode = expected_type in (dict, list)
    prompt = enrich_for_json(
        prompt,
        json_mode=json_mode,
        expects_list=(expected_type is list),
    )

    def _try_registry(current_prompt: str, attempt_label: str,
                      *, model_name: str | None = None):
        t0 = time.time()
        try:
            endpoint_hint = describe_target(
                agent_name,
                json_mode=json_mode,
                model_name=model_name,
                cfg=cfg,
            )
        except Exception:
            endpoint_hint = "unknown endpoint"
        try:
            llm_resp = invoke_prompt(
                agent_name=agent_name,
                prompt=current_prompt,
                max_tokens=max_tokens,
                json_mode=json_mode,
                cfg=cfg,
                model_name=model_name,
            )
            ms = int((time.time() - t0) * 1000)
            if pg_logger:
                pg_logger.log_llm_call(
                    agent_name=f"{agent_name}[{attempt_label}]",
                    stage=stage,
                    provider=llm_resp.provider,
                    model=llm_resp.model,
                    prompt_tokens=llm_resp.prompt_tokens,
                    completion_tokens=llm_resp.completion_tokens,
                    duration_ms=ms,
                    success=True,
                )
            data = _parse_llm_output(
                llm_resp.content,
                agent_name=agent_name,
                attempt_label=attempt_label,
                expected_type=expected_type,
                required_keys=required_keys,
            )
            return data, None
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            logger.warning(
                "llm_call [%s/%s] → %s: %s",
                agent_name, attempt_label, endpoint_hint, exc,
            )
            if pg_logger:
                pg_logger.log_llm_call(
                    agent_name=f"{agent_name}[{attempt_label}]",
                    stage=stage,
                    provider="registry",
                    model=model_name or "auto",
                    prompt_tokens=0,
                    completion_tokens=0,
                    duration_ms=ms,
                    success=False,
                    error_message=f"{endpoint_hint}: {exc}",
                )
            return None, str(exc)

    current_prompt = prompt
    last_error = ""
    for attempt in range(1, max_retries + 1):
        if last_error:
            current_prompt = (
                f"{prompt}\n\n[Previous attempt failed: {last_error}. "
                "Fix the JSON format and try again.]"
            )
        data, err = _try_registry(current_prompt, f"p{attempt}")
        if data is not None:
            return data
        last_error = err or "unknown error"

    registry_keys = set(load_model_registry().keys())
    default_model = load_agent_model_map().get("__default__")
    if default_model and default_model not in registry_keys:
        default_model = None
    try:
        primary_model = resolve_registry_model_name(agent_name)
    except RuntimeError:
        primary_model = None
    if default_model and default_model != primary_model:
        data, _err = _try_registry(
            prompt, "default-fallback", model_name=default_model,
        )
        if data is not None:
            return data

    logger.error("llm_call [%s]: registry attempts failed, returning fallback", agent_name)
    return fallback


# ── Core guardrail dispatcher ─────────────────────────────────────────────────

def _llm_call(
    cfg,
    prompt: str,
    fallback: Any,
    agent_name: str,
    provider: str = "anthropic",
    expected_type: type = dict,
    required_keys: list[str] | None = None,
    stage: str = "enrich_stage",
    max_retries: int = 2,
    max_tokens: int = 4096,
    retry_on_truncation: bool = False,
) -> Any:
    """
    Dual-provider LLM call with guardrails.

    Guardrail order:
      1. Primary provider, attempt 1
      2. Primary provider, attempt 2 (prompt includes previous error)
      3. Fallback provider, attempt 1
      4. Return `fallback` if all fail

    When ``retry_on_truncation`` is set, a response that stops because it hit the
    output token cap is re-issued with a larger budget (up to the model max)
    BEFORE any lossy salvage — so dense steps never silently drop rows.
    """
    from langchain_core.messages import HumanMessage
    from uhc_llm import is_registry_backend

    if is_registry_backend():
        return _llm_call_via_registry(
            cfg, prompt, fallback, agent_name,
            expected_type=expected_type,
            required_keys=required_keys,
            stage=stage,
            max_retries=max_retries,
            max_tokens=max_tokens,
        )

    pg_logger = getattr(cfg, "_pg_logger", None)

    # claude-sonnet-4-5 / gpt-4o both support large completions; this is the
    # ceiling we escalate toward when a response is truncated.
    _MAX_TOKEN_CEILING = 32000

    forced = _forced_provider()
    if forced:
        provider = forced

    def _stopped_on_length(resp) -> bool:
        meta = getattr(resp, "response_metadata", None) or {}
        reason = str(meta.get("stop_reason") or meta.get("finish_reason") or "").lower()
        return reason in {"max_tokens", "length"}

    def _try(make_llm_fn, prov_name, model_name, current_prompt, attempt_label):
        t0 = time.time()
        try:
            budget = max_tokens
            llm  = make_llm_fn(cfg, max_tokens=budget)
            resp = llm.invoke([HumanMessage(content=current_prompt)])
            # Escalate the token budget if the model ran out of room mid-JSON,
            # rather than salvaging a partial (row-dropping) payload.
            while (retry_on_truncation and _stopped_on_length(resp)
                   and budget < _MAX_TOKEN_CEILING):
                budget = min(budget * 2, _MAX_TOKEN_CEILING)
                logger.warning(
                    "llm_call [%s/%s]: output hit token cap — retrying at %d tokens",
                    agent_name, attempt_label, budget,
                )
                llm  = make_llm_fn(cfg, max_tokens=budget)
                resp = llm.invoke([HumanMessage(content=current_prompt)])
            inp, out = _token_usage(resp)
            ms = int((time.time() - t0) * 1000)
            if pg_logger:
                pg_logger.log_llm_call(
                    agent_name=f"{agent_name}[{attempt_label}]",
                    stage=stage, provider=prov_name, model=model_name,
                    prompt_tokens=inp, completion_tokens=out,
                    duration_ms=ms, success=True,
                )
            data = _parse_llm_output(
                resp.content,
                agent_name=agent_name,
                attempt_label=attempt_label,
                expected_type=expected_type,
                required_keys=required_keys,
            )
            return data, None
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            logger.warning("llm_call [%s/%s]: %s", agent_name, attempt_label, exc)
            if pg_logger:
                pg_logger.log_llm_call(
                    agent_name=f"{agent_name}[{attempt_label}]",
                    stage=stage, provider=prov_name, model=model_name,
                    prompt_tokens=0, completion_tokens=0,
                    duration_ms=ms, success=False, error_message=str(exc),
                )
            return None, str(exc)

    # When a provider is forced, "fallback" is one extra attempt on the SAME
    # provider instead of crossing over to the (broken) other one.
    alt_provider = provider if forced else (
        "openai" if provider == "anthropic" else "anthropic")
    primary_fn   = _make_anthropic_llm if provider  == "anthropic" else _make_openai_llm
    fallback_fn  = _make_openai_llm   if alt_provider == "openai"  else _make_anthropic_llm
    primary_model  = cfg.anthropic_model if provider     == "anthropic" else cfg.openai_model
    fallback_model = cfg.openai_model    if alt_provider == "openai"    else cfg.anthropic_model

    # Provider-specific JSON instructions (direct API-key path).
    from uhc_llm.prompts import ARRAY_WRAP_SUFFIX, enrich_for_json

    if provider == "anthropic":
        prompt = enrich_for_json(prompt, json_mode=True)
    elif provider == "openai" and expected_type is list:
        prompt = prompt + ARRAY_WRAP_SUFFIX

    current_prompt = prompt
    last_error = ""
    for attempt in range(1, max_retries + 1):
        if last_error:
            current_prompt = (
                f"{prompt}\n\n[Previous attempt failed: {last_error}. "
                "Fix the JSON format and try again.]"
            )
        data, err = _try(primary_fn, provider, primary_model,
                         current_prompt, f"p{attempt}")
        if data is not None:
            return data
        last_error = err or "unknown error"

    # Cross-provider fallback
    fb_prompt = enrich_for_json(prompt, json_mode=(alt_provider == "anthropic"))
    data, err = _try(fallback_fn, alt_provider, fallback_model, fb_prompt, "fallback")
    if data is not None:
        return data

    logger.error("llm_call [%s]: all attempts failed, returning fallback", agent_name)
    return fallback


# ── Native-PDF (vision) call path ─────────────────────────────────────────────
# Registry bedrock models use gateway ``/model/{deployment}/invoke``; direct
# API-key mode uses Anthropic ``/v1/messages`` with document blocks. PDF bytes
# go through ``uhc_llm.router.invoke_pdf`` (see ``_llm_call_pdf`` below).
#
# Page-image raster fallback (scanned PDFs) still uses LangChain vision models
# directly via ``_llm_call_images`` — there is no registry wrapper for image
# blocks yet.


def _make_anthropic_vision_llm(cfg, max_tokens: int = 8192):
    """A higher-token ChatAnthropic for document perception (pages are dense)."""
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=cfg.anthropic_model,
        api_key=cfg.anthropic_api_key,
        max_tokens=max_tokens,
        temperature=0,
    )


def _make_openai_vision_llm(cfg, max_tokens: int = 8192):
    """A higher-token ChatOpenAI (gpt-4o family) for multimodal page-image
    perception. Keeps json_object mode so the contract matches the text path."""
    from langchain_openai import ChatOpenAI
    from uhc_llm.backend import DEFAULT_OPENAI_MODEL

    return ChatOpenAI(
        model=cfg.openai_model or DEFAULT_OPENAI_MODEL,
        api_key=cfg.openai_api_key,
        max_tokens=max_tokens,
        temperature=0,
        model_kwargs={"response_format": {"type": "json_object"}},
    )


def _image_content_blocks(prompt: str, image_b64_list: list[str], provider: str) -> list[dict]:
    """Build a provider-specific multimodal message body: image blocks first,
    then the instruction text. Anthropic and OpenAI use different image schemas."""
    blocks: list[dict] = []
    for b64 in image_b64_list:
        if not b64:
            continue
        if provider == "openai":
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        else:
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            })
    blocks.append({"type": "text", "text": prompt})
    return blocks


def _llm_call_images(
    cfg,
    prompt: str,
    image_b64_list: list[str],
    fallback: Any,
    agent_name: str,
    provider: str = "openai",
    expected_type: type = dict,
    required_keys: list[str] | None = None,
    stage: str = "pdf_perceive",
    max_tokens: int = 8192,
) -> Any:
    """Multimodal (page-image) LLM call with the same guardrails as
    ``_llm_call_pdf``. Sends rasterised page/band images to a vision model
    (``provider`` = "openai" gpt-4o, or "anthropic" Claude) and parses → salvages
    → schema-validates the JSON. This is the any-PDF safety net: it works for
    scanned/image-only PDFs and oversized pages that the native-PDF text path
    cannot read. Returns ``fallback`` on total failure.
    """
    from langchain_core.messages import HumanMessage
    pg_logger = getattr(cfg, "_pg_logger", None)
    if provider not in {"openai", "anthropic"}:
        provider = "openai"
    model_name = cfg.openai_model if provider == "openai" else cfg.anthropic_model
    base_prompt = prompt + (
        "\n\nIMPORTANT: Reply with valid JSON only. No markdown, no explanation."
    )

    def _build():
        return (_make_openai_vision_llm(cfg, max_tokens) if provider == "openai"
                else _make_anthropic_vision_llm(cfg, max_tokens))

    def _try(current_prompt: str, attempt_label: str):
        t0 = time.time()
        try:
            llm = _build()
            content = _image_content_blocks(current_prompt, image_b64_list, provider)
            resp = llm.invoke([HumanMessage(content=content)])
            inp, out = _token_usage(resp)
            ms = int((time.time() - t0) * 1000)
            if pg_logger:
                pg_logger.log_llm_call(
                    agent_name=f"{agent_name}[{attempt_label}]",
                    stage=stage, provider=provider, model=model_name,
                    prompt_tokens=inp, completion_tokens=out,
                    duration_ms=ms, success=True,
                )
            raw = resp.content if isinstance(resp.content, str) \
                else _coerce_text_content(resp.content)
            try:
                data = _parse_json(raw)
            except json.JSONDecodeError as je:
                salvaged = (_salvage_json_object(raw) if expected_type is dict
                            else _salvage_json_array(raw))
                if not salvaged:
                    raise
                logger.warning("llm_call_images [%s/%s]: salvaged from truncated JSON (%s)",
                               agent_name, attempt_label, je)
                data = salvaged
            data = _unwrap_if_needed(data, expected_type)
            if not _validate_schema(data, expected_type, required_keys):
                raise ValueError(
                    f"Schema mismatch: expected {expected_type.__name__} "
                    f"with keys {required_keys}"
                )
            return data, None
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            logger.warning("llm_call_images [%s/%s]: %s", agent_name, attempt_label, exc)
            if pg_logger:
                pg_logger.log_llm_call(
                    agent_name=f"{agent_name}[{attempt_label}]",
                    stage=stage, provider=provider, model=model_name,
                    prompt_tokens=0, completion_tokens=0,
                    duration_ms=ms, success=False, error_message=str(exc),
                )
            return None, str(exc)

    last_error = ""
    for attempt in range(1, 3):
        label = f"attempt{attempt}"
        p = base_prompt if attempt == 1 else (
            base_prompt + f"\n\nYour previous reply failed: {last_error}. Return valid JSON only.")
        data, err = _try(p, label)
        if data is not None:
            return data
        last_error = err or "unknown"
    return fallback


def _llm_call_pdf(
    cfg,
    prompt: str,
    pdf_b64_list: list[str],
    fallback: Any,
    agent_name: str,
    expected_type: type = dict,
    required_keys: list[str] | None = None,
    stage: str = "pdf_perceive",
    max_retries: int = 2,
    max_tokens: int = 8192,
) -> Any:
    """PDF vision call with the same guardrails as ``_llm_call`` (both backends)."""
    from uhc_llm.prompts import enrich_for_json
    from uhc_llm.router import describe_target, invoke_pdf

    pg_logger = getattr(cfg, "_pg_logger", None)
    base_prompt = enrich_for_json(prompt, json_mode=True)

    def _try(current_prompt: str, attempt_label: str):
        t0 = time.time()
        try:
            endpoint_hint = describe_target(agent_name, pdf=True, cfg=cfg)
        except Exception:
            endpoint_hint = "unknown endpoint"
        try:
            llm_resp = invoke_pdf(
                agent_name=agent_name,
                prompt=current_prompt,
                pdf_b64_list=pdf_b64_list,
                max_tokens=max_tokens,
                cfg=cfg,
            )
            ms = int((time.time() - t0) * 1000)
            if pg_logger:
                pg_logger.log_llm_call(
                    agent_name=f"{agent_name}[{attempt_label}]",
                    stage=stage,
                    provider=llm_resp.provider,
                    model=llm_resp.model,
                    prompt_tokens=llm_resp.prompt_tokens,
                    completion_tokens=llm_resp.completion_tokens,
                    duration_ms=ms,
                    success=True,
                )
            data = _parse_llm_output(
                llm_resp.content,
                agent_name=agent_name,
                attempt_label=attempt_label,
                expected_type=expected_type,
                required_keys=required_keys,
            )
            return data, None
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            logger.warning(
                "llm_call_pdf [%s/%s] → %s: %s",
                agent_name, attempt_label, endpoint_hint, exc,
            )
            if pg_logger:
                pg_logger.log_llm_call(
                    agent_name=f"{agent_name}[{attempt_label}]",
                    stage=stage,
                    provider="llm",
                    model="auto",
                    prompt_tokens=0,
                    completion_tokens=0,
                    duration_ms=ms,
                    success=False,
                    error_message=f"{endpoint_hint}: {exc}",
                )
            return None, str(exc)

    last_error = ""
    for attempt in range(1, max_retries + 1):
        current = base_prompt
        if last_error:
            current = (
                f"{base_prompt}\n\n[Previous attempt failed: {last_error}. "
                "Fix the JSON format and try again.]"
            )
        data, err = _try(current, f"p{attempt}")
        if data is not None:
            return data
        last_error = err or "unknown error"

    logger.error("llm_call_pdf [%s]: all attempts failed, returning fallback", agent_name)
    return fallback


# ── 0. StepChecklistReconcilerAgent — Anthropic ───────────────────────────────
# Consumes the deterministic step_inventory context store built by BS4 in
# a03.html_step_inventory. Every step number on the checklist MUST end up in
# `steps`. Missing steps are sent (raw cell grid) to the LLM for structured
# extraction; if the LLM fails, a deterministic row→rule fallback guarantees
# the step is still materialised. This is what prevents an entire SOP from
# collapsing into a single "Step 0" node.

_TERMINAL_TOKENS = ("(f3)", "(f4)", "process the claim", "save the claim")

_VALID_DECISIONS = {"DENY", "ALLOW", "BYPASS", "PEND", "WAIVE", "REFER",
                    "STOP", "SYSTEM", "CONDITIONAL", "OVERRIDE", "NOTE",
                    "ELIGIBILITY"}


def _blank_step(num: int, question: str, raw_text: str) -> dict:
    return {
        "number": num, "question": question, "intro_text": "",
        "decision_rows": [], "annotations": [],
        "branch_yes": "", "branch_no": "",
        "skip_to_step_yes": None, "skip_to_step_no": None,
        "referenced_sops": [],
        "is_terminal": any(t in raw_text.lower() for t in _TERMINAL_TOKENS),
        "raw_text": raw_text, "source_html": "",
    }


def _row_to_decision(num: int, cells: list[str]) -> dict | None:
    from .a03_parse_html import _codes, _guess_decision, _skip_to
    cells = [c for c in cells if c.strip()]
    if cells and cells[0].strip() == str(num):
        cells = cells[1:]
    if not cells:
        return None
    if len(cells) == 1:
        cond_if, cond_and, action = "", "", cells[0]
    elif len(cells) == 2:
        cond_if, cond_and, action = cells[0], "", cells[1]
    else:
        cond_if, cond_and, action = cells[0], cells[1], " ".join(cells[2:])
    joined = " ".join(cells)
    return {
        "condition_if": cond_if[:500], "condition_and": cond_and[:500],
        "action": action[:1000], "decision": _guess_decision(joined),
        "codes": _codes(joined), "skip_to_step": _skip_to(joined),
        "routing_label": "",
    }


def _inventory_fallback_step(entry: dict) -> dict:
    """Deterministic conversion of one inventory entry into a step dict."""
    num = entry["number"]
    step = _blank_step(num, entry.get("title", ""), entry.get("raw_text", ""))
    rows = entry.get("rows", [])
    # First row that merely restates the title/question is not a rule
    for i, r in enumerate(rows):
        cells = r.get("cells", [])
        if i == 0 and len([c for c in cells if c.strip()
                           and c.strip() != str(num)]) == 1:
            continue
        d = _row_to_decision(num, cells)
        if d:
            step["decision_rows"].append(d)
    return step


def step_checklist_reconciler(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    inventory = state.get("step_inventory") or []
    if not inventory:
        return {}
    steps = list(state.get("steps") or [])
    have = {s.get("number") for s in steps}
    missing = [e for e in inventory if e["number"] not in have]
    if not missing:
        return {}

    logger.info("step_checklist_reconciler: checklist=%s extracted=%s missing=%s",
                [e["number"] for e in inventory], sorted(have),
                [e["number"] for e in missing])

    payload = [{
        "number": e["number"],
        "title": (e.get("title") or "")[:200],
        "rows": [r["cells"] for r in e.get("rows", [])][:40],
    } for e in missing[:25]]

    prompt = f"""You are converting a healthcare claims SOP step/action table into structured audit steps.

Each input step below was parsed from HTML tables. "rows" is the raw cell grid
(columns are usually If… / And… / Then…, or Yes/No branches, or a single action).

For EVERY input step return one object — do not skip or merge any step number:
{{
  "number": <same step number>,
  "question": "<the step's question or action, clear active voice>",
  "is_terminal": true/false  (true only for final process/save actions like F3/F4),
  "decision_rows": [{{
     "condition_if":  "<IF condition — '' if the row is an unconditional action>",
     "condition_and": "<AND condition — '' if none>",
     "action":        "<THEN action the auditor must take, complete and specific>",
     "decision":      "DENY|ALLOW|BYPASS|PEND|WAIVE|REFER|STOP|SYSTEM|CONDITIONAL"
  }}]
}}

Rules:
- Header rows (If/And/Then) are not decision rows.
- A row that just restates the question is not a decision row.
- Keep every code (EX CODE OCA, F3, W46…) and every timeframe (90 days, 365 days…) verbatim in the action text.

Return a JSON array, one object per step.

Steps:
{json.dumps(payload, indent=2)}"""

    result = _llm_call(cfg, prompt, [], "step_checklist_reconciler",
                       provider="anthropic", expected_type=list,
                       required_keys=["number"], max_tokens=8192)

    by_num: dict[int, dict] = {}
    if isinstance(result, list):
        for r in result:
            try:
                by_num[int(r.get("number"))] = r
            except (TypeError, ValueError):
                continue

    new_steps = []
    for e in missing:
        num = e["number"]
        llm = by_num.get(num)
        if llm and isinstance(llm.get("decision_rows"), list):
            step = _blank_step(num, (llm.get("question") or e.get("title", ""))[:500],
                               e.get("raw_text", ""))
            if isinstance(llm.get("is_terminal"), bool):
                step["is_terminal"] = llm["is_terminal"] or step["is_terminal"]
            from .a03_parse_html import _codes, _guess_decision, _skip_to
            for row in llm["decision_rows"]:
                if not isinstance(row, dict):
                    continue
                action = str(row.get("action") or "").strip()
                if not action:
                    continue
                decision = str(row.get("decision") or "").upper()
                if decision not in _VALID_DECISIONS:
                    decision = _guess_decision(action)
                step["decision_rows"].append({
                    "condition_if": str(row.get("condition_if") or "")[:500],
                    "condition_and": str(row.get("condition_and") or "")[:500],
                    "action": action[:1000], "decision": decision,
                    "codes": _codes(action), "skip_to_step": _skip_to(action),
                    "routing_label": "",
                })
            # LLM returned the step but no usable rows → deterministic fallback
            if not step["decision_rows"]:
                step = _inventory_fallback_step(e)
                step["question"] = (llm.get("question") or step["question"])[:500]
        else:
            step = _inventory_fallback_step(e)
        new_steps.append(step)

    merged = sorted(steps + new_steps, key=lambda s: s.get("number", 0))
    logger.info("step_checklist_reconciler: backfilled %d steps → total %d",
                len(new_steps), len(merged))
    return {"steps": merged}


# ── 1. StepQuestionRefinerAgent — Anthropic ───────────────────────────────────

_QSTOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "in", "on",
    "for", "and", "or", "if", "this", "that", "your", "you", "it", "as", "at",
    "by", "with", "from", "any", "all", "do", "does", "did", "determine",
    "perform", "following", "below", "current", "claim", "step", "review",
}


def _q_content_tokens(text: str) -> set[str]:
    import re as _re

    toks = _re.findall(r"[a-z0-9]+", (text or "").lower())
    return {t for t in toks if t not in _QSTOP and len(t) > 2}


def _safe_question_rewrite(original: str, rewrite: str) -> str:
    """Accept a refined question ONLY if it introduces no new content word that
    is absent from the original (prevents hallucinated re-wordings such as
    "Member county / Applies-To field" -> "member's line of business"). On any
    drift, fall back to the verbatim original. This is deterministic and never
    fabricates meaning."""
    rewrite = (rewrite or "").strip()
    original = (original or "").strip()
    if not rewrite:
        return original
    orig_tokens = _q_content_tokens(original)
    new_tokens = _q_content_tokens(rewrite)
    introduced = new_tokens - orig_tokens
    # A faithful cleanup may reorder/drop filler but must not invent new content
    # nouns. Allow at most a single incidental new token (e.g. a pluralisation
    # the stopword filter missed); reject anything more as a meaning change.
    if len(introduced) > 1:
        return original
    return rewrite


def step_question_refiner(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("steps") or []
    if not steps:
        return {}
    raw_questions = [
        {"number": s["number"],
         "raw": (s.get("question") or s.get("raw_text",""))[:300]}
        for s in steps[:20]
    ]
    prompt = f"""You are a healthcare claims policy analyst tidying SOP step
questions for display. This is FAITHFUL copy-editing, NOT rewriting.

STRICT RULES (a wrong question is worse than an ugly one):
• Preserve the EXACT meaning and every specific noun, field name, code, system
  name, place, and condition from the original.
• You may only fix capitalization/spacing, drop a redundant leading verb, or
  turn a fragment into a question. Do NOT introduce ANY word or concept that is
  not already in the original. Do NOT generalize, guess intent, or invent.
• If you cannot improve it without changing meaning, return the original verbatim.

Return JSON array only: [{{"number": N, "question": "lightly cleaned question"}}]

Steps:
{json.dumps(raw_questions, indent=2)}"""

    result = _llm_call(cfg, prompt, [], "step_question_refiner",
                       provider="anthropic", expected_type=list,
                       required_keys=["number", "question"])
    if not isinstance(result, list):
        return {}
    q_map = {r["number"]: r.get("question", "") for r in result if "number" in r}
    enriched = []
    for s in steps:
        original = s.get("question", "")
        proposed = q_map.get(s["number"], original)
        enriched.append(dict(s, question=_safe_question_rewrite(original, proposed)))
    return {"enriched_steps": enriched}


# ── 2. DecisionRowClassifierAgent — Anthropic ─────────────────────────────────

def decision_row_classifier(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("enriched_steps") or state.get("steps") or []
    all_rows = []
    for s in steps:
        for j, row in enumerate(s.get("decision_rows", [])):
            all_rows.append({
                "step": s["number"], "row": j,
                "if":     row.get("condition_if", ""),
                "action": row.get("action", ""),
                "current": row.get("decision", "CONDITIONAL"),
            })
    if not all_rows:
        return {}

    batch = all_rows[:20]
    prompt = f"""Classify each healthcare claim processing decision rule.

Valid decisions: DENY, ALLOW, BYPASS, PEND, REFER, SYSTEM, STOP, WAIVE, CONDITIONAL

Return JSON array: [{{"step": N, "row": N, "decision": "DENY", "rationale": "brief reason"}}]

Rules to classify:
{json.dumps(batch, indent=2)}"""

    result = _llm_call(cfg, prompt, [], "decision_row_classifier",
                       provider="anthropic", expected_type=list,
                       required_keys=["step", "row", "decision"])
    if not isinstance(result, list):
        return {}
    decision_map = {(r["step"], r["row"]): r["decision"] for r in result if "step" in r}
    enriched = []
    for s in steps:
        s2 = dict(s)
        rows = []
        for j, row in enumerate(s.get("decision_rows", [])):
            r2 = dict(row)
            r2["decision"] = decision_map.get((s["number"], j), row.get("decision", "CONDITIONAL"))
            rows.append(r2)
        s2["decision_rows"] = rows
        enriched.append(s2)
    return {"enriched_steps": enriched}


# ── 3. RuleSemanticEnricherAgent — Anthropic ──────────────────────────────────

def rule_semantic_enricher(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("enriched_steps") or state.get("steps") or []
    sample = []
    for s in steps[:5]:
        for j, row in enumerate(s.get("decision_rows", [])[:4]):
            sample.append({
                "step": s["number"], "row": j,
                "action": row.get("action", ""),
            })
    if not sample:
        return {}

    prompt = f"""For each healthcare claims rule action, extract:
- action_line: line-level override (e.g. "reduce to $0", "bypass duplicate edit")
- action_claim: claim-level outcome (e.g. "deny claim", "allow claim", "pend for review")

Return JSON array: [{{"step": N, "row": N, "action_line": "...", "action_claim": "..."}}]

Rules:
{json.dumps(sample, indent=2)}"""

    result = _llm_call(cfg, prompt, [], "rule_semantic_enricher",
                       provider="anthropic", expected_type=list,
                       required_keys=["step", "row", "action_line", "action_claim"])
    if not isinstance(result, list):
        return {}
    enrich_map = {(r["step"], r["row"]): r for r in result if "step" in r}
    enriched = []
    for s in steps:
        s2 = dict(s)
        rows = []
        for j, row in enumerate(s.get("decision_rows", [])):
            r2 = dict(row)
            extra = enrich_map.get((s["number"], j), {})
            r2["action_line"]  = extra.get("action_line", "")
            r2["action_claim"] = extra.get("action_claim", "")
            rows.append(r2)
        s2["decision_rows"] = rows
        enriched.append(s2)
    return {"enriched_steps": enriched}


# ── 4. CrossReferenceResolverAgent — OpenAI ───────────────────────────────────

def cross_reference_resolver(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("enriched_steps") or state.get("steps") or []
    known = [d.get("title", "") for d in (state.get("all_documents") or []) if d.get("title")]
    refs  = list({r for s in steps for r in s.get("referenced_sops", [])})
    if not refs:
        return {}

    prompt = f"""Match each SOP reference text to the closest known document title.
If no match, set matched to null.

Return JSON array: [{{"ref": "...", "matched": "title or null", "confidence": 0.0}}]

References: {json.dumps(refs[:15])}
Known documents: {json.dumps(known[:30])}"""

    _llm_call(cfg, prompt, [], "cross_reference_resolver",
              provider="openai", expected_type=list, required_keys=["ref"])
    return {}  # enrichment stored in future graph edges, not state


# ── 5. AmbiguousTermResolverAgent — Anthropic ─────────────────────────────────

def ambiguous_term_resolver(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    meta     = state.get("metadata") or {}
    raw_text = (state.get("raw_text") or "")[:3000]
    prompt = f"""Resolve vague terms in this healthcare SOP to their specific meaning.

SOP Title: {meta.get('title', '')}
Platform:  {meta.get('platform', '')}

Excerpt:
{raw_text}

Return JSON object:
{{
  "the_plan":        "specific plan name or type",
  "the_group":       "specific group or entity",
  "member_submitted":"what this means in context",
  "other_terms":     {{"term": "meaning"}}
}}"""

    result = _llm_call(cfg, prompt, {}, "ambiguous_term_resolver",
                       provider="anthropic", expected_type=dict,
                       required_keys=["the_plan"])
    if not isinstance(result, dict):
        return {}
    meta2 = dict(meta)
    meta2["resolved_terms"] = result
    return {"metadata": meta2}


# ── 6. POTFValidatorAgent — Anthropic ────────────────────────────────────────

def potf_validator(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("enriched_steps") or state.get("steps") or []
    step_summary = [
        {"step": s["number"],
         "question": s.get("question", ""),
         "decisions": [r.get("decision") for r in s.get("decision_rows", [])]}
        for s in steps
    ]
    prompt = f"""Review these SOP steps for a healthcare timely filing / duplicate claim policy.

Analyse completeness:
- Are all claim scenarios covered?
- Any missing DENY or ALLOW branches?
- Any gaps or contradictions?

Return JSON:
{{
  "complete": true,
  "gaps": ["description of any gap"],
  "warnings": ["policy concern"],
  "coverage_pct": 95
}}

Steps:
{json.dumps(step_summary[:20], indent=2)}"""

    result = _llm_call(cfg, prompt,
                       {"complete": True, "gaps": [], "warnings": [], "coverage_pct": 100},
                       "potf_validator",
                       provider="anthropic", expected_type=dict,
                       required_keys=["complete", "warnings"])
    if isinstance(result, dict):
        warnings = list(state.get("validation_warnings") or [])
        warnings.extend(result.get("warnings", []))
        return {"validation_warnings": warnings}
    return {}


# ── 7. PreSectionRuleExtractorAgent — OpenAI ─────────────────────────────────

def pre_section_rule_extractor(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Extract structured audit rules from every pre-section block.

    Each rule becomes a structured decision entry (condition → action → type).
    Sections with substantive exception/override logic are flagged with
    is_exception_block=True so the write layer can promote them to the
    decision tree as a "Step 0 — Pre-Step Exceptions" AuditStep.
    """
    pre = state.get("pre_sections") or []
    if not pre:
        return {}

    # Per-section input budget. Large preamble blocks (cross-billing intros,
    # provider-exclusion lists, "Duplicate Exceptions") must not be input-
    # truncated, so this is generous.
    PER_SECTION_CHARS = 12000
    # Process every section — segmenting the PDF preamble into named sections
    # can yield well over the old 15-section cap, and dropping the tail would
    # silently lose whole sections (e.g. "Duplicate Exceptions").
    section_texts = []
    for s in pre[:60]:
        raw_text = " ".join(
            (i.get("text", str(i)) if isinstance(i, dict) else str(i))
            for i in s.get("items", [])
        )
        section_texts.append({
            "name": s.get("name", ""),
            "text": raw_text[:PER_SECTION_CHARS],
        })

    # Batch sections so each LLM call's OUTPUT stays well under the token cap.
    # A single all-sections call truncated mid-array on big SOPs (the JSON was
    # cut off at ~13 KB → unrecoverable on every retry). Splitting by an input
    # char budget bounds the response size; a long section becomes its own batch.
    BATCH_CHAR_BUDGET = 5000
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_len = 0
    for sec in section_texts:
        slen = len(sec["text"]) + len(sec["name"])
        if cur and cur_len + slen > BATCH_CHAR_BUDGET:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(sec)
        cur_len += slen
    if cur:
        batches.append(cur)

    _PROMPT_HEAD = """You are reading a healthcare claims SOP as a senior claims auditor.

The sections below appear BEFORE the numbered decision-tree steps. They contain:
• Eligibility/applicability rules (who this SOP covers)
• Exception and override rules (when standard steps do NOT apply)
• Billing-specific rules (e.g. monthly vs per-diem vs 15-minute case services)
• Cross-billing rules, timely-filing rules, code descriptions

For EACH distinct, actionable rule you find:
1. Write it as a standalone IF condition → THEN action pair.
2. Assign decision_type: DENY | ALLOW | BYPASS | OVERRIDE | ELIGIBILITY | REFER | NOTE
3. Flag is_exception=true if the rule says "exclude", "do not apply", "bypass", or overrides normal steps.
4. OPERATIVE IDENTIFIER LISTS — DO NOT SUMMARISE. When a rule relies on an explicit
   list of identifiers (provider TINs/NPIs, provider/facility names, group/plan
   names, Tax IDs, or codes that are INCLUDED IN or EXCLUDED FROM the process — e.g.
   a "Virgin Island Providers excluded from cross-billing" TIN/Provider table), you
   MUST NOT collapse it into "TIN is one of: 128380004, ...". Keep the gate as the
   parent condition/action, and put EVERY list entry in a ``sub_rules`` array — one
   object per entry — with the identifier verbatim in ``condition`` and the matching
   name/value verbatim in ``action`` (e.g. {"condition": "128380004", "action":
   "NAYER, ANNE D"}). Never drop a provider name and never merge two entries.

Return a JSON array — one object per rule:
[{
  "section": "<exact section name from input>",
  "condition": "<complete IF condition — be specific, include codes/values>",
  "action": "<complete THEN action — what the auditor must do>",
  "decision_type": "DENY|ALLOW|BYPASS|OVERRIDE|ELIGIBILITY|REFER|NOTE",
  "is_exception": true/false,
  "sub_rules": [{"condition": "<identifier verbatim>", "action": "<name/value verbatim>"}]
}]
``sub_rules`` is optional — include it ONLY for rules that carry an identifier list;
omit it (or use []) otherwise.

Only return the JSON array. No prose. Extract every individual rule — do not combine
them. If the SAME rule appears more than once because the input text is duplicated,
output that rule only ONCE (do not emit duplicate condition/action pairs).

Sections:
"""

    result: list = []
    for batch in batches:
        prompt = _PROMPT_HEAD + json.dumps(batch, indent=2)
        part = _llm_call(cfg, prompt, [], "pre_section_rule_extractor",
                         provider="openai", expected_type=list,
                         required_keys=["section", "condition", "action"],
                         max_tokens=8192)
        if isinstance(part, list):
            result.extend(part)

    if not result:
        return {}

    pre2 = [dict(s) for s in pre]
    name_map = {s.get("name", ""): s for s in pre2}

    for rule in result:
        sec_name = rule.get("section", "")
        # Match by exact name first, then substring
        target = name_map.get(sec_name)
        if not target:
            for s in pre2:
                if sec_name in s.get("name", "") or s.get("name", "") in sec_name:
                    target = s
                    break
        if target:
            target.setdefault("llm_rules", []).append(rule)

    # Mark sections that contain exception/override/eligibility rules
    for s in pre2:
        rules = s.get("llm_rules", [])
        s["is_exception_block"] = any(
            r.get("is_exception") or r.get("decision_type") in
            ("DENY", "ALLOW", "BYPASS", "OVERRIDE", "ELIGIBILITY")
            for r in rules
        )

    return {"pre_sections": pre2}


# ── 8. GroupRuleExtractorAgent — OpenAI (was group_rule_nlp) ─────────────────

def group_rule_extractor(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    group_rules = state.get("group_rules") or []
    if not group_rules:
        return {}
    sample = [
        {"group": gr.get("group_name", ""),
         "raw_text": gr.get("raw_text", "")[:400]}
        for gr in group_rules[:15]
    ]
    prompt = f"""Extract precise timely filing limits for each group from this healthcare SOP.

Return JSON array:
[{{
  "group":       "group name",
  "inn_days":    90,
  "oon_days":    180,
  "from":        "DOS or PAID_DATE or EOB_DATE",
  "exceptions":  ["exception description"],
  "notes":       "any special conditions"
}}]

Groups:
{json.dumps(sample, indent=2)}"""

    result = _llm_call(cfg, prompt, [], "group_rule_extractor",
                       provider="openai", expected_type=list,
                       required_keys=["group"])
    if not isinstance(result, list):
        return {}
    result_map = {r["group"]: r for r in result if "group" in r}
    updated = []
    for gr in group_rules:
        gr2 = dict(gr)
        extra = result_map.get(gr.get("group_name", ""), {})
        if extra.get("inn_days") and not gr2.get("limit_days"):
            gr2["limit_days"] = extra["inn_days"]
        if extra.get("inn_days"):
            gr2["inn_days"] = extra["inn_days"]
        if extra.get("oon_days"):
            gr2["oon_days"] = extra["oon_days"]
        if extra.get("from"):
            gr2["calculation_from"] = extra["from"]
        if extra.get("exceptions"):
            gr2["exceptions"] = extra["exceptions"]
        updated.append(gr2)
    return {"group_rules": updated}


# ── 9. DateConditionExtractorAgent — OpenAI (was date_condition_nlp) ──────────

def date_condition_extractor(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    raw = (state.get("raw_text") or "")[:5000]
    if not raw.strip():
        return {}
    prompt = f"""Extract all date-based conditions and effective ranges from this healthcare SOP.

Look for patterns like:
- "claims processed on or after MM/DD/YYYY"
- "DOS between MM/DD/YYYY - MM/DD/YYYY"
- "effective as of MM/DD/YYYY"
- "for dates of service prior to MM/DD/YYYY"

Return JSON array (empty array if none found):
[{{
  "date_from":      "MM/DD/YYYY or null",
  "date_to":        "MM/DD/YYYY or null",
  "effective_date": "MM/DD/YYYY or null",
  "context":        "surrounding sentence",
  "source_field":   "section name or step number"
}}]

Text:
{raw}"""

    result = _llm_call(cfg, prompt, [], "date_condition_extractor",
                       provider="openai", expected_type=list)
    if not isinstance(result, list):
        return {}
    existing = list(state.get("detected_date_conditions") or [])
    existing.extend(result)
    return {"detected_date_conditions": existing}


# ── 10. SummaryGeneratorAgent — Anthropic ────────────────────────────────────

def summary_generator(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    meta  = state.get("metadata") or {}
    steps = state.get("enriched_steps") or state.get("steps") or []
    pre   = state.get("pre_sections") or []
    grps  = state.get("group_rules") or []

    prompt = f"""Write a comprehensive executive summary of this healthcare SOP.

Title:    {meta.get('title', 'Unknown')}
Platform: {meta.get('platform', '')}
LOB:      {meta.get('lob', [])}
Steps:    {len(steps)}
Pre-sections: {[s.get('name','') for s in pre[:5]]}
Group rules:  {[g.get('group_name','') for g in grps[:5]]}
Key decisions:{[s.get('question','') for s in steps[:5]]}

Return JSON:
{{
  "summary":      "3-4 sentence executive summary",
  "purpose":      "one sentence stating what this SOP governs",
  "key_rules":    ["top 3-5 rules as bullet points"],
  "coverage":     "what claims / groups / dates this applies to"
}}"""

    result = _llm_call(cfg, prompt,
                       {"summary": "", "purpose": "", "key_rules": [], "coverage": ""},
                       "summary_generator",
                       provider="anthropic", expected_type=dict,
                       required_keys=["summary"])
    return {"llm_summary": result.get("summary", "") if isinstance(result, dict) else ""}


# ── Backward-compatibility alias (graph.py imports this name) ─────────────────
# Keep old names pointing to new implementations so graph.py needs no change
# for the NLP→extractor rename.
group_rule_nlp       = group_rule_extractor
date_condition_nlp   = date_condition_extractor
