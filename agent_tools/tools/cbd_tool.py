"""
CBD (Covered Benefit Document) coverage tool — slim repo-local port.

Public tool: ``check_medicare_coverage``.

Side-effect cbd_api_info() that the upstream module calls at import is
gated by the AGENT_TOOLS_LAZY_LOAD env flag — we never run it at import.
"""
from __future__ import annotations

import os
import time
from typing import Any

from langchain_core.tools import StructuredTool

from ._cache import ToolCache
from ._http import request_json
from ._logging import get_logger
from .schemas.cbd import CBDCoverageInput

LOGGER = get_logger("cbd")
_CACHE = ToolCache(name="cbd", default_ttl=900)


def _token() -> str:
    cached = _CACHE.get("token", "default")
    if cached:
        return cached
    url = os.environ["CBD_TOKEN_URL"]
    body = {
        "client_id": os.environ.get("CBD_CLIENT_ID", "mock"),
        "client_secret": os.environ.get("CBD_CLIENT_SECRET", "mock"),
        "grant_type": "client_credentials",
    }
    _, resp, _ = request_json("POST", url, json=body)
    token = resp.get("access_token") or "mock-cbd-token"
    _CACHE.set("token", "default", token, ttl=300)
    return token


def _check_coverage(**kwargs: Any) -> dict[str, Any]:
    cpt_codes = [c.strip().upper() for c in (kwargs.get("cpt_codes") or []) if c]
    group_name = kwargs.get("group_name") or "Standard Medicare"
    plan_name = kwargs.get("plan_name") or "Standard Medicare"
    claim_id = kwargs.get("claim_id")

    if not cpt_codes:
        return {
            "success": False,
            "group_name": group_name,
            "plan_name": plan_name,
            "total_codes_queried": 0,
            "codes_found": 0,
            "coverage_details": [],
            "not_found_codes": [],
            "errors": ["No CPT codes supplied"],
        }

    token = _token()
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "msid": "mock",
        "lobs": ["UHC M&R"] if "medicare" in group_name.lower() else ["COMMERCIAL"],
        "markets": ["ALL"],
        "products": [],
        "custNames": [group_name],
        "plans": [plan_name],
        "advancedFilters": {"cptCodes": cpt_codes, "claimId": claim_id},
    }
    status, resp, _ = request_json(
        "POST", os.environ["CBD_API_URL"], headers=headers, json=body,
    )
    if status >= 400:
        return {
            "success": False,
            "group_name": group_name,
            "plan_name": plan_name,
            "total_codes_queried": len(cpt_codes),
            "codes_found": 0,
            "coverage_details": [],
            "not_found_codes": cpt_codes,
            "errors": [resp.get("error") or f"HTTP {status}"],
        }
    details = resp.get("coverage_details") or []
    found_codes = {d.get("cpt_code") for d in details if d.get("cpt_code")}
    not_found = [c for c in cpt_codes if c not in found_codes]
    return {
        "success": True,
        "group_name": group_name,
        "plan_name": plan_name,
        "total_codes_queried": len(cpt_codes),
        "codes_found": len(details),
        "coverage_details": details,
        "not_found_codes": not_found,
        "errors": [],
    }


def build_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="check_medicare_coverage",
        description=(
            "Check CPT coverage in the Covered Benefit Document (CBD) API. "
            "Resolves Line of Business from claim_id when provided."
        ),
        func=lambda **kwargs: _check_coverage(**kwargs),
        args_schema=CBDCoverageInput,
    )
