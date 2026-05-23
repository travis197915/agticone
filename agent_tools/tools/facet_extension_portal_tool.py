"""
Facet Extension Portal family of tools — slim repo-local port.

Three tools:

* ``facet_extension_portal_provider`` — full BH provider list for a PRPR ID.
* ``facet_extension_portal_programme`` — single programme by detailed ID.
* ``facet_ext_portal_group_model`` — group model (e.g. "1A") for a claim.

The group-model URL is read from ``FEP_GROUP_MODEL_BASE_URL`` (mockable),
not hardcoded to staging like the upstream copy.
"""
from __future__ import annotations

import os
import time
from typing import Any

from langchain_core.tools import StructuredTool

from ._cache import ToolCache
from ._http import request_json
from ._logging import get_logger
from .schemas.fep import GroupModelInput, ProgrammeInput, ProviderInput

LOGGER = get_logger("fep")
_CACHE = ToolCache(name="fep", default_ttl=600)


def _base() -> str:
    return os.environ["FACET_EXTENSION_PORTAL_BASE_URL"].rstrip("/")


def _group_base() -> str:
    return os.environ.get(
        "FEP_GROUP_MODEL_BASE_URL",
        # Mock-friendly default; upstream hardcoded a staging host. We
        # honor the env var to make the mock interception trivial.
        "http://localhost:8000/api/mocks/fep/checkModel",
    ).rstrip("/")


def _provider(provider_id: str) -> dict[str, Any]:
    cached = _CACHE.get("provider", provider_id)
    if cached:
        return cached
    url = f"{_base()}/getCompleteList/{provider_id}"
    try:
        status, body, _ = request_json("GET", url)
    except Exception as exc:
        return {
            "success": False,
            "provider_id": provider_id,
            "error": str(exc),
            "message": f"Failed to retrieve data for provider ID: {provider_id}",
        }
    if status >= 400:
        return {
            "success": False,
            "provider_id": provider_id,
            "status_code": status,
            "error": body.get("error") or body.get("message") or f"HTTP {status}",
            "message": f"Failed to retrieve data for provider ID: {provider_id}",
        }
    out = {
        "success": True,
        "provider_id": provider_id,
        "status_code": status,
        "data": body,
    }
    _CACHE.set("provider", provider_id, out)
    return out


def _programme(program_detailed_id: str) -> dict[str, Any]:
    cached = _CACHE.get("programme", program_detailed_id)
    if cached:
        return cached
    url = f"{_base()}/getPrgm/{program_detailed_id}"
    try:
        status, body, _ = request_json("GET", url)
    except Exception as exc:
        return {
            "success": False,
            "program_detailed_id": program_detailed_id,
            "error": str(exc),
            "message": f"Failed to retrieve programme {program_detailed_id}",
        }
    out = {
        "success": status < 400,
        "program_detailed_id": program_detailed_id,
        "status_code": status,
        "data": body,
    }
    if status < 400:
        _CACHE.set("programme", program_detailed_id, out)
    return out


def _group_model(claim_number: str) -> dict[str, Any]:
    # Resolve PRPR_ID via the facets summary mock.
    from .facets_tool import _summary as facets_summary
    summary = facets_summary(claim_number).get("body") or {}
    cs = summary.get("Data", {}).get("ClaimSummary", {})
    prpr_id = cs.get("PRPR_ID") or summary.get("PRPR_ID")
    if not prpr_id:
        return {
            "success": False,
            "status_code": 0,
            "meta_data": "Could not resolve PRPR_ID from claim summary",
            "prpr_id": None,
            "group_model": None,
            "claim_number": claim_number,
            "endpoint": "group_model",
            "timestamp": int(time.time()),
            "error": "missing PRPR_ID",
        }
    url = f"{_group_base()}/{prpr_id}"
    try:
        status, body, _ = request_json("GET", url)
    except Exception as exc:
        return {
            "success": False,
            "status_code": 0,
            "meta_data": f"Group model lookup failed for provider {prpr_id}",
            "prpr_id": prpr_id,
            "group_model": None,
            "claim_number": claim_number,
            "endpoint": "group_model",
            "timestamp": int(time.time()),
            "error": str(exc),
            "message": "connection error",
        }
    return {
        "success": status < 400,
        "status_code": status,
        "meta_data": f"Group model lookup for provider {prpr_id}",
        "prpr_id": prpr_id,
        "group_model": body.get("group_model") or body.get("groupModel"),
        "claim_number": claim_number,
        "endpoint": "group_model",
        "timestamp": int(time.time()),
    }


def build_tools() -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            name="facet_extension_portal_provider",
            description="Retrieve full BH provider list for a PRPR ID.",
            func=lambda provider_id: _provider(provider_id),
            args_schema=ProviderInput,
        ),
        StructuredTool.from_function(
            name="facet_extension_portal_programme",
            description="Get a single programme's details for the supplied Program Detailed ID.",
            func=lambda program_detailed_id: _programme(program_detailed_id),
            args_schema=ProgrammeInput,
        ),
        StructuredTool.from_function(
            name="facet_ext_portal_group_model",
            description=(
                "Resolve PRPR_ID via Facets summary, then fetch network "
                "fee-schedule group model string (e.g. '1A')."
            ),
            func=lambda claim_number: _group_model(claim_number),
            args_schema=GroupModelInput,
        ),
    ]
