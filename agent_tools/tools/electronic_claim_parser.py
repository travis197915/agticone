"""
Electronic claim parser (regex-first) — slim repo-local port
(``claim_parse_flat_template_with_confidence``).

Returns a payload shaped like the LLM parser's output so downstream
consumers can swap providers transparently. The deterministic mock
extraction here is intentionally minimal — it pulls a handful of fields
out of the print-image text if it can locate them and otherwise emits a
shaped-but-empty payload.
"""
from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import StructuredTool

from ._logging import get_logger
from .schemas.llm_claim import ClaimParseFlatTemplateInput

LOGGER = get_logger("electronic_claim_parser")


# Tiny set of regexes mirroring the upstream "Box 21 diagnoses" and totals
# extraction. Sufficient for the smoke tests; full extraction is out of
# scope for the registry/runtime build.
_DIAG_RE = re.compile(r"\b\d\s+([A-Z]\d{2}[A-Z0-9]?)\b")
_TOTAL_CHARGE_RE = re.compile(r"TOTAL\s+CHARGE\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I)


def _content_to_text(claim_data: dict[str, Any]) -> str:
    content = claim_data.get("content") or claim_data.get("body") or ""
    if isinstance(content, (dict, list)):
        return str(content)
    return str(content)


def _parse(**kwargs: Any) -> dict[str, Any]:
    claim_data = kwargs.get("claim_data") or {}
    template_name = kwargs.get("template_name") or "emc_medical"
    text = _content_to_text(claim_data)

    try:
        diagnoses = []
        for idx, match in enumerate(_DIAG_RE.finditer(text), start=1):
            diagnoses.append({
                "pointer": idx, "code": match.group(1),
                "provenance": {"evidence": match.group(0)},
            })
        m = _TOTAL_CHARGE_RE.search(text)
        total_charge = float(m.group(1)) if m else 0.0
        return {
            "template_name": template_name,
            "claim_source": "physician",
            "fields": {},
            "diagnoses": diagnoses,
            "line_items": [],
            "totals": {
                "total_charge": total_charge,
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
            "confidence_scores": {
                "overall": 0.5 if diagnoses else 0.0,
                "field_level": {},
            },
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": {"code": "PARSE_FAILED", "message": str(exc)},
            "template_name": template_name,
        }


def build_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="claim_parse_flat_template_with_confidence",
        description=(
            "Deterministic regex-first HCFA print-image parser. Returns a "
            "payload shape compatible with the LLM parser."
        ),
        func=lambda **kwargs: _parse(**kwargs),
        args_schema=ClaimParseFlatTemplateInput,
    )
