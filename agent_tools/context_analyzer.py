"""LLM-backed understanding of an MCP tool response.

Given the raw payload an MCP tool returns, produce a structured *context*:

* ``summary``     — one-paragraph plain-English description of the payload.
* ``fields``      — per-field ``{name, path, type, example, description}`` rows
                    explaining what each key means in claims terms.
* ``record_count``— number of records when the payload is a list.
* ``truncated``   — whether the analyzed payload was a salvaged/truncated JSON
                    string (the mock server caps some cells at 32k chars).
* ``sample_response`` — the trimmed representative sample actually analyzed.

The heavy lifting reuses the engine's guarded dual-provider helper
(``uhc_execution_engine.llm.llm_call``: Claude primary, GPT-4o fallback). When
neither provider is configured we still return a deterministic structural
analysis (field names + inferred types + examples) so the feature degrades
gracefully without keys.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Keep the sample small so the analysis stays cheap and fast regardless of how
# large the underlying payload is.
_MAX_SAMPLE_RECORDS = 3
_MAX_STRING_CHARS = 400


# ── Payload coercion ─────────────────────────────────────────────────────────


def _salvage_truncated(text: str) -> Any | None:
    """Recover the largest valid prefix of a truncated JSON array/object.

    Mirrors the frontend ``JsonTree`` salvage: walk back to the last complete
    ``}``, close the container, and re-parse. Returns ``None`` when nothing can
    be recovered.
    """
    t = text.strip()
    if len(t) < 2 or t[0] not in "[{":
        return None
    closer = "]" if t[0] == "[" else ""
    end = t.rfind("}")
    attempts = 0
    while attempts < 8 and end > 0:
        candidate = t[: end + 1].rstrip().rstrip(",")
        if t[0] == "[":
            candidate += closer
        try:
            return json.loads(candidate)
        except Exception:
            end = t.rfind("}", 0, end)
            attempts += 1
    return None


def _coerce_payload(result: Any) -> tuple[Any, bool]:
    """Return ``(parsed_payload, truncated)``.

    Unwraps the common ``{"raw": "<json-string>"}`` envelope the mock server
    uses and parses (or salvages) the embedded JSON so we analyze real records
    rather than an opaque string.
    """
    truncated = False
    payload = result

    # Unwrap a single-key {"raw": "..."} envelope.
    if isinstance(payload, dict) and set(payload.keys()) == {"raw"} and isinstance(payload["raw"], str):
        payload = payload["raw"]

    if isinstance(payload, str):
        s = payload.strip()
        if s[:1] in "[{":
            try:
                payload = json.loads(s)
            except Exception:
                salvaged = _salvage_truncated(s)
                if salvaged is not None:
                    payload = salvaged
                    truncated = True
    return payload, truncated


def _trim(value: Any) -> Any:
    """Shrink a value for the LLM prompt: cap list length and string size."""
    if isinstance(value, list):
        return [_trim(v) for v in value[:_MAX_SAMPLE_RECORDS]]
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in value.items()}
    if isinstance(value, str) and len(value) > _MAX_STRING_CHARS:
        return value[:_MAX_STRING_CHARS] + "…"
    return value


def _py_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _structural_fields(sample_record: Any) -> list[dict[str, Any]]:
    """Deterministic field list (no descriptions) used as the fallback."""
    if not isinstance(sample_record, dict):
        return [{"name": "(value)", "path": "$", "type": _py_type(sample_record),
                 "example": _trim(sample_record), "description": ""}]
    fields: list[dict[str, Any]] = []
    for key, val in sample_record.items():
        fields.append({
            "name": key,
            "path": key,
            "type": _py_type(val),
            "example": _trim(val),
            "description": "",
        })
    return fields


# ── Public entry point ───────────────────────────────────────────────────────


def analyze_response(tool_name: str, description: str, result: Any) -> dict[str, Any]:
    """Produce a structured understanding of ``result`` for ``tool_name``."""
    payload, truncated = _coerce_payload(result)

    record_count: int | None = None
    sample_record: Any = payload
    if isinstance(payload, list):
        record_count = len(payload)
        sample_record = payload[0] if payload else {}

    sample = _trim(payload)
    fallback_fields = _structural_fields(sample_record)
    fallback = {
        "summary": (
            f"{tool_name} returned "
            + (f"an array of {record_count} records." if record_count is not None
               else f"a {_py_type(payload)} payload.")
        ),
        "fields": fallback_fields,
    }

    analysis = fallback
    provider = ""
    model = ""
    try:
        from uhc_execution_engine.config import get_config
        from uhc_execution_engine.llm import llm_call

        cfg = get_config()
        if cfg.anthropic_api_key or cfg.openai_api_key:
            prompt = _build_prompt(tool_name, description, sample, record_count, truncated)
            data, meta = llm_call(
                cfg, prompt,
                agent_name="mcp_context_analyzer",
                stage="mcp_tool_understanding",
                fallback=fallback,
                provider="anthropic" if cfg.anthropic_api_key else "openai",
                expected_type=dict,
                required_keys=["summary", "fields"],
            )
            if isinstance(data, dict) and data.get("fields"):
                analysis = data
                provider = meta.get("provider", "")
                model = meta.get("model", "")
        else:
            logger.info("mcp_context_analyzer: no LLM keys configured; using structural fallback")
    except Exception as exc:  # pragma: no cover - never break the test flow
        logger.warning("mcp_context_analyzer LLM step failed: %s", exc)

    fields = analysis.get("fields") or fallback_fields
    if not isinstance(fields, list):
        fields = fallback_fields

    return {
        "summary": str(analysis.get("summary") or fallback["summary"]),
        "fields": [_normalize_field(f) for f in fields if isinstance(f, dict)],
        "record_count": record_count,
        "truncated": truncated,
        "sample_response": sample,
        "llm_provider": provider,
        "llm_model": model,
    }


def _normalize_field(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(f.get("name") or f.get("path") or ""),
        "path": str(f.get("path") or f.get("name") or ""),
        "type": str(f.get("type") or ""),
        "example": f.get("example", ""),
        "description": str(f.get("description") or ""),
    }


def _build_prompt(tool_name: str, description: str, sample: Any,
                  record_count: int | None, truncated: bool) -> str:
    sample_json = json.dumps(sample, default=str)[:6000]
    count_note = (
        f"The full payload is an array of {record_count} records; "
        f"only the first {_MAX_SAMPLE_RECORDS} are shown below as a sample.\n"
        if record_count is not None else ""
    )
    trunc_note = (
        "Note: the source payload was truncated by the upstream server, so the "
        "sample is a recovered prefix.\n" if truncated else ""
    )
    return (
        "You are a claims-data analyst. You are given a sample of the JSON "
        f"response from an MCP tool named '{tool_name}'"
        + (f" ({description})" if description else "")
        + ".\n"
        + count_note
        + trunc_note
        + "Explain what this data is and document every field.\n\n"
        "Return a JSON object with exactly these keys:\n"
        '  "summary": a 1-3 sentence plain-English description of the whole payload '
        "(what it represents, in health-insurance claims terms).\n"
        '  "fields": an array where each item is an object with keys '
        '"name" (the field key), "path" (dotted path within a record), '
        '"type" (string|number|boolean|array|object|null), '
        '"example" (a representative value from the sample), and '
        '"description" (what the field means and, where obvious, what its '
        "values signify — e.g. coverage flags, dates, codes).\n\n"
        "Cover ALL distinct fields you can see in the sample records. "
        "Be concise and concrete.\n\n"
        f"SAMPLE RESPONSE:\n{sample_json}"
    )
