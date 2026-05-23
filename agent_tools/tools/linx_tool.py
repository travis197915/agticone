"""LINX BH claim search tool — slim repo-local port (``linx_claim_search``)."""
from __future__ import annotations

import json
import os
from typing import Any

from langchain_core.tools import StructuredTool

from ._cache import ToolCache
from ._http import request_json
from ._logging import get_logger
from .schemas.linx import LinxClaimSearchInput

LOGGER = get_logger("linx")
_CACHE = ToolCache(name="linx", default_ttl=24 * 3600)


def _token() -> str:
    cached = _CACHE.get("token", "default")
    if cached:
        return cached
    body = {
        "client_id": os.environ.get("LINX_CLIENT_ID", "mock"),
        "client_secret": os.environ.get("LINX_CLIENT_SECRET", "mock"),
        "grant_type": "client_credentials",
    }
    _, resp, _ = request_json("POST", os.environ["LINX_AUTH_URL"], json=body)
    token = resp.get("access_token") or "mock-linx-token"
    ttl = int(resp.get("expires_in") or 3600)
    _CACHE.set("token", "default", token, ttl=max(ttl - 60, 60))
    return token


def _claim_search(**kwargs: Any) -> dict[str, Any]:
    subscriber_id = kwargs.get("subscriber_id") or kwargs.get("subscriberId")
    if not subscriber_id:
        return {
            "success": False, "data": None,
            "error": "subscriber_id is required", "cache_hit": False,
        }
    cache_key = json.dumps(kwargs, sort_keys=True, default=str)
    cached = _CACHE.get("search", cache_key)
    if cached:
        return {**cached, "cache_hit": True}

    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
        "bhRequestHeader": json.dumps({
            "applicationId": "obhagenticai",
            "dataSource": os.environ.get("LINX_DATASOURCE", "prod"),
        }),
    }
    body = {
        "subscriberId": subscriber_id,
        "firstName": kwargs.get("first_name") or kwargs.get("firstName"),
        "lastName": kwargs.get("last_name") or kwargs.get("lastName"),
        "dob": kwargs.get("dob"),
        "startDate": kwargs.get("start_date") or kwargs.get("startDate"),
        "endDate": kwargs.get("end_date") or kwargs.get("endDate"),
        "unetPolicyNbr": kwargs.get("unet_policy_nbr") or kwargs.get("unetPolicyNbr"),
        "claimMaxLimit": kwargs.get("claim_max_limit") or kwargs.get("claimMaxLimit") or 0,
        "externalAccountIdList": kwargs.get("external_account_id_list")
            or kwargs.get("externalAccountIdList") or [],
    }
    try:
        status, resp, _ = request_json(
            "POST", os.environ["LINX_API_URL"], headers=headers, json=body,
        )
    except Exception as exc:
        return {"success": False, "data": None, "error": str(exc), "cache_hit": False}

    if status >= 400:
        return {
            "success": False, "data": None,
            "error": f"LINX API error: HTTP {status}", "cache_hit": False,
        }
    if isinstance(resp, list):
        data = {"results": resp}
    else:
        data = resp
    out = {"success": True, "data": data, "error": None, "cache_hit": False}
    _CACHE.set("search", cache_key, out)
    return out


def build_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="linx_claim_search",
        description=(
            "Search BH claims in LINX by subscriber ID (optionally narrowed "
            "by name, DOB, date range, UNET policy, external account IDs)."
        ),
        func=lambda **kwargs: _claim_search(**kwargs),
        args_schema=LinxClaimSearchInput,
    )
