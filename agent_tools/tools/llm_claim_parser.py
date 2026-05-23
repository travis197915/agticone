"""
LLM claim parser tool — slim repo-local port (``llm_parse_claim_with_ontology``).

When ``AGENT_TOOLS_LLM_MOCK=true`` (default in this build), no real LLM
call is made; the tool returns a deterministic, schema-shaped skeleton so
the registry/invoke surface can be smoke-tested end to end without Azure.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

from langchain_core.tools import StructuredTool

from ._cache import ToolCache
from ._logging import get_logger
from .schemas.llm_claim import LlmClaimParseInput

LOGGER = get_logger("llm_claim_parser")
_CACHE = ToolCache(name="llm_claim_parser", default_ttl=3600)


def _mock_invoke(prompt: str, template_name: str) -> dict[str, Any]:
    """Deterministic stub used while AGENT_TOOLS_LLM_MOCK is on."""
    return {
        "template_name": template_name,
        "claim_source": "physician",
        "fields": {
            "_input_hash": {
                "value": hashlib.sha1(prompt.encode("utf-8")).hexdigest(),
                "confidence": 1.0,
                "provenance": {"evidence": "deterministic mock"},
            }
        },
        "diagnoses": [],
        "line_items": [],
        "totals": {
            "total_charge": 0.0,
            "total_patient_paid": 0.0,
            "total_other_insurance": {"paid": None, "allowed": None},
        },
        "other_insurance": {
            "plan_name": None, "claim_number": None,
            "total_oi_paid": 0.0, "remark_codes": [],
        },
        "hcp_pricing": {
            "icn": None, "price_method": None,
            "repriced_allowed_amt": 0.0, "reject_code": "",
        },
        "unmapped_fields": {},
        "confidence_scores": {"overall": 0.0, "field_level": {}},
    }


def _content_to_text(claim_data: dict[str, Any]) -> str:
    content = claim_data.get("content")
    if content is None and "body" in claim_data:
        content = claim_data["body"]
    if isinstance(content, (dict, list)):
        return str(content)
    return str(content or "")


def _parse(**kwargs: Any) -> dict[str, Any]:
    claim_data = kwargs.get("claim_data") or {}
    template_name = kwargs.get("template_name") or "emc_medical"
    text = _content_to_text(claim_data)
    cache_key = hashlib.sha256((template_name + "::" + text).encode("utf-8")).hexdigest()
    cached = _CACHE.get("parse", cache_key)
    if cached:
        return cached

    if (os.environ.get("AGENT_TOOLS_LLM_MOCK", "true") or "true").strip().lower() in {"1", "true", "yes"}:
        result = _mock_invoke(text, template_name)
        _CACHE.set("parse", cache_key, result)
        return result

    # Real LLM path would call Azure / OpenAI here. For now we still return
    # the mock skeleton with a flag so callers can detect "no LLM configured".
    result = _mock_invoke(text, template_name)
    result["_error"] = "Real LLM client is not configured in this build"
    return result


def build_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="llm_parse_claim_with_ontology",
        description=(
            "LLM-first HCFA extraction with ontology + JSON Schema. In "
            "mock mode (AGENT_TOOLS_LLM_MOCK=true) returns a deterministic "
            "schema-shaped skeleton."
        ),
        func=lambda **kwargs: _parse(**kwargs),
        args_schema=LlmClaimParseInput,
    )
