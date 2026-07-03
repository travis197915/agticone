"""Shared prompt enrichment for JSON-structured LLM calls."""
from __future__ import annotations

JSON_ONLY_SUFFIX = (
    "\n\nIMPORTANT: Reply with valid JSON only. No markdown, no explanation."
)
ARRAY_WRAP_SUFFIX = '\n\nWrap the array in a JSON object: {"items": [...]}'


def enrich_for_json(
    prompt: str,
    *,
    json_mode: bool,
    expects_list: bool = False,
) -> str:
    """Append JSON guardrail suffixes used by ingestion agents and router."""
    if not json_mode:
        return prompt
    out = prompt + JSON_ONLY_SUFFIX
    if expects_list:
        out += ARRAY_WRAP_SUFFIX
    return out
