"""
Facets family of tools — slim repo-local port.

Exposes seven LangChain tools:

* ``facets_get_summary``
* ``facets_get_cob``
* ``facets_get_line_details``
* ``facets_get_member_eligibility``
* ``facets_get_provider_details``
* ``facets_get_duplicate_claim``

All call the same upstream Facets server (mock in dev).
"""
from __future__ import annotations

import os
import time
from typing import Any

from langchain_core.tools import StructuredTool

from ._cache import ToolCache
from ._http import request_json
from ._logging import get_logger
from .schemas.facets import ClaimNumberInput, ProviderDetailsInput

LOGGER = get_logger("facets")
_CACHE = ToolCache(name="facets", default_ttl=600)
_TOKEN_TTL = 50 * 60   # 50 min in-process
_MAX_LINE_SEQ = int(os.environ.get("MAX_LINE_SEQ", "100"))


def _facets_base() -> str:
    return os.environ["FACETS_BASE_URL"].rstrip("/")


def _facets_token() -> str:
    cached = _CACHE.get("token", "default")
    if cached:
        return cached
    body = {
        "username": os.environ.get("FACETS_USERNAME", "mock"),
        "password": os.environ.get("FACETS_PASSWORD", "mock"),
        "region": os.environ.get("FACETS_REGION", "us"),
        "identity": os.environ.get("FACETS_IDENTITY", "mock"),
        "signonMethod": os.environ.get("FACETS_SIGNON_METHOD", "mock"),
    }
    status, resp, _ = request_json(
        "POST", f"{_facets_base()}/security/tokens", json=body,
    )
    if status >= 400:
        return ""
    token = resp.get("access_token") or resp.get("token") or "mock-facets-token"
    _CACHE.set("token", "default", token, ttl=_TOKEN_TTL)
    return token


def _envelope(
    endpoint: str,
    claim_number: str,
    status_code: int,
    body: Any,
    *,
    status_message: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status_code": status_code,
        "status_message": status_message,
        "body": body,
        "claim_number": claim_number,
        "endpoint": endpoint,
        "timestamp": int(time.time()),
    }
    if extra:
        out.update(extra)
    return out


def _get(endpoint_path: str, claim_number: str, endpoint_label: str) -> dict[str, Any]:
    cache_key = f"{endpoint_label}:{claim_number}"
    cached = _CACHE.get("get", cache_key)
    if cached:
        return cached
    token = _facets_token()
    if not token:
        return {
            "error": "Token error",
            "message": "Token error",
            "status_code": None,
            "status_message": None,
            "claim_number": claim_number,
            "endpoint": endpoint_label,
            "timestamp": int(time.time()),
            "token_status": {"status": "failed"},
        }
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{_facets_base()}{endpoint_path}"
    status, body, _ = request_json("GET", url, headers=headers)
    envelope = _envelope(endpoint_label, claim_number, status, body)
    if status == 200:
        _CACHE.set("get", cache_key, envelope)
    return envelope


# ── Individual tools ─────────────────────────────────────────────────────────


def _summary(claim_number: str) -> dict[str, Any]:
    return _get(
        f"/Claims/{claim_number}/Inquiry/Summary", claim_number, "summary",
    )


def _cob(claim_number: str) -> dict[str, Any]:
    return _get(
        f"/Claims/{claim_number}/Inquiry/COB", claim_number, "cob",
    )


def _line_details(claim_number: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    first_missing = 0
    for seq in range(1, _MAX_LINE_SEQ + 1):
        env = _get(
            f"/Claims/{claim_number}/Inquiry/Lines/{seq}/Details",
            claim_number,
            f"line_details:{seq}",
        )
        sc = env.get("status_code")
        if sc == 404 or (sc is not None and sc >= 400):
            first_missing = seq
            break
        items.append({
            "line_seq": seq,
            "status_code": sc,
            "body": env.get("body"),
        })
    return {
        "claim_number": claim_number,
        "endpoint": "line_details",
        "timestamp": int(time.time()),
        "first_missing_line_seq": first_missing or None,
        "total_lines": len(items),
        "items": items,
    }


def _member_eligibility(claim_number: str) -> dict[str, Any]:
    summary = _summary(claim_number)
    body = summary.get("body") or {}
    # Cheap MEME_CK probe — the mock returns it explicitly under Data.ClaimSummary.
    meme_ck = (
        body.get("MEME_CK")
        or body.get("Data", {}).get("ClaimSummary", {}).get("MEME_CK")
        or body.get("Data", {}).get("ClaimSummary", {}).get("REC_CIV8", {}).get("MEME_CK")
    )
    if not meme_ck:
        return {
            "error": "Could not extract MEME_CK",
            "claim_number": claim_number,
            "endpoint": "member_eligibility",
        }
    return _get(
        f"/Members/Coverage/MemberKey/{meme_ck}/Eligibility",
        claim_number,
        "member_eligibility",
    )


def _provider_details(**kwargs) -> dict[str, Any]:
    cn = kwargs["claim_number_for_reference"]
    entity = kwargs.get("provider_entity_type")
    tax = kwargs.get("tax_id")

    # Auto-resolve when missing using summary mocks.
    if not entity or not tax:
        summary = _summary(cn).get("body") or {}
        cs = summary.get("Data", {}).get("ClaimSummary", {})
        entity = entity or cs.get("PRPR_ENTITY") or "P"
        tax = tax or cs.get("MCTN_ID") or cs.get("PRPR_TIN") or ""

    if entity and entity not in {"P", "G", "I", "F"}:
        return {
            "error": f"invalid provider_entity_type: {entity}",
            "claim_number_for_reference": cn,
            "endpoint": "provider_details",
            "timestamp": int(time.time()),
        }

    token = _facets_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "procedure": "CMCSP_PRV1_SRCH_PRPR_NAME_REMT",
        "parameters": {
            "PRPR_ENTITY": entity,
            "MCTN_ID": tax,
            "name": "%",
            "city": "%",
            "state": "%",
            "zip": "%",
        },
    }
    status, resp, _ = request_json(
        "POST", f"{_facets_base()}/data/procedure/execute",
        headers=headers, json=body,
    )
    rows = (
        resp.get("Data", {}).get("ResultSets", [{}])[0].get("Rows", [])
        if isinstance(resp, dict) else []
    )
    return {
        "status_code": status,
        "status_message": None,
        "body": resp,
        "provider_entity_type": entity,
        "tax_id": tax,
        "claim_number_for_reference": cn,
        "doc360_rendering_npis": [],
        "filter_applied": False,
        "rows_before_filter": len(rows),
        "rows_after_filter": len(rows),
        "filter_skipped_reason": "no doc360 NPIs in mock invocation",
        "endpoint": "provider_details",
        "timestamp": int(time.time()),
    }


def _duplicate_claim(claim_number: str) -> dict[str, Any]:
    summary = _summary(claim_number).get("body") or {}
    cs = summary.get("Data", {}).get("ClaimSummary", {})
    sub = cs.get("SBSB_ID")
    grp = cs.get("GRGR_ID")
    ctype = cs.get("CLCL_CL_SUB_TYPE")
    if not (sub and grp and ctype):
        return {
            "error": "Missing search keys",
            "claim_number": claim_number,
            "endpoint": "duplicate_claim",
            "timestamp": int(time.time()),
            "extracted": {"SBSB_ID": sub, "GRGR_ID": grp, "CLCL_CL_SUB_TYPE": ctype},
        }
    token = _facets_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{_facets_base()}/Search/Claims/Inquiry"
    params = {"SubscriberID": sub, "GroupID": grp, "ClaimType": ctype}
    status, resp, _ = request_json("GET", url, headers=headers, params=params)
    candidates = (resp or {}).get("Data", {}).get("Claims", [])

    # Single-line filter per the docs: pull line 1 details.
    line1 = _get(
        f"/Claims/{claim_number}/Inquiry/Lines/1/Details",
        claim_number, "line_details:1",
    ).get("body") or {}
    low = line1.get("CLCL_LOW_SVC_DT") or ""
    high = line1.get("CLCL_HIGH_SVC_DT") or ""
    filtered = [
        c for c in candidates
        if low <= (c.get("CDML_FROM_DT") or "") and (c.get("CDML_TO_DT") or "") <= high
    ] if low and high else candidates

    return {
        "status_code": status,
        "status_message": None,
        "claim_number": claim_number,
        "endpoint": "duplicate_claim",
        "timestamp": int(time.time()),
        "search_params": params,
        "total_claims_found_before_filtering": len(candidates),
        "total_line_items": 1,
        "line_items": [{
            "CDML_SEQ_NO": 1,
            "CDML_FROM_DT": low,
            "CDML_TO_DT": high,
            "filtered_claims_count": len(filtered),
            "filtered_claims": filtered,
        }],
    }


# ── StructuredTool builders ──────────────────────────────────────────────────


def build_tools() -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            name="facets_get_summary",
            description="Fetch comprehensive Facets claim summary by claim number.",
            func=lambda claim_number: _summary(claim_number),
            args_schema=ClaimNumberInput,
        ),
        StructuredTool.from_function(
            name="facets_get_cob",
            description="Fetch Coordination of Benefits data from Facets.",
            func=lambda claim_number: _cob(claim_number),
            args_schema=ClaimNumberInput,
        ),
        StructuredTool.from_function(
            name="facets_get_line_details",
            description="Fetch ALL service-line details by iterating Lines/{seq}/Details.",
            func=lambda claim_number: _line_details(claim_number),
            args_schema=ClaimNumberInput,
        ),
        StructuredTool.from_function(
            name="facets_get_member_eligibility",
            description="Resolve MEME_CK from the summary then fetch member eligibility.",
            func=lambda claim_number: _member_eligibility(claim_number),
            args_schema=ClaimNumberInput,
        ),
        StructuredTool.from_function(
            name="facets_get_provider_details",
            description=(
                "Look up provider rows via CMCSP_PRV1_SRCH_PRPR_NAME_REMT. "
                "Auto-resolves entity/tin from the Facets summary when omitted."
            ),
            func=lambda **kwargs: _provider_details(**kwargs),
            args_schema=ProviderDetailsInput,
        ),
        StructuredTool.from_function(
            name="facets_get_duplicate_claim",
            description=(
                "Find potential duplicates by (SubscriberID, GroupID, ClaimType) "
                "extracted from the summary, filtered by line DOS range."
            ),
            func=lambda claim_number: _duplicate_claim(claim_number),
            args_schema=ClaimNumberInput,
        ),
    ]
