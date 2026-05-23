"""
Local stub of ``claim_micro_image_id_to_fln_dcc_doc360_parse``.

The upstream orchestrator chains
``get_claim_summary → read_claim_by_fln_dcc → claim_parse_flat_template_with_confidence``.
This module re-implements just enough of that flow so the registry-driven
invoke surface can run without the full thynkr extract.

It is **not** registered as a LangChain tool in the seed catalog — kept as
a Python-callable for the FacetsProvider tool's auto-resolve flow.
"""
from __future__ import annotations

from typing import Any


def claim_micro_image_id_to_fln_dcc_doc360_parse(claim_number: str) -> dict[str, Any]:
    from .doc360_tool import _doc360_read
    from .electronic_claim_parser import _parse as regex_parse
    from .facets_tool import _summary

    summary = _summary(claim_number)
    fln = (
        summary.get("body", {}).get("Data", {}).get("ClaimSummary", {}).get("CLCL_ID")
        or summary.get("body", {}).get("CLCL_ID")
        or claim_number
    )
    envelope = _doc360_read(str(fln))
    parsed = regex_parse(claim_data=envelope, template_name="emc_medical")
    return {
        "claim_number": claim_number,
        "fln_dcc": fln,
        "envelope": envelope,
        "parsed": parsed,
    }
