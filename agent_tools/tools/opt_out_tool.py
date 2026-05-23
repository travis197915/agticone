"""CMS Medicare opt-out checker tool — slim repo-local port (``medicare_optout_checker``)."""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from langchain_core.tools import StructuredTool

from ._http import request_json
from ._logging import get_logger
from .schemas.optout import ProviderOptOutInput

LOGGER = get_logger("optout")


def _format_record(record: dict[str, Any]) -> dict[str, Any]:
    first = record.get("First Name", "N/A")
    last = record.get("Last Name", "N/A")
    npi = str(record.get("NPI", "N/A"))
    eff = record.get("Optout Effective Date", "N/A")
    end = record.get("Optout End Date", "N/A")
    renewal = record.get("Last updated", "N/A")
    try:
        end_dt = datetime.strptime(end, "%m/%d/%Y") if end != "N/A" else None
        if end_dt is None:
            status = "Unknown"
        elif datetime.now() <= end_dt:
            status = "Yes"
        else:
            status = "No (Expired)"
    except (ValueError, TypeError):
        status = "Unknown"
    return {
        "provider_name": f"{first} {last}",
        "npi": npi,
        "optout_effective_date": eff,
        "optout_end_date": end,
        "optout_status": status,
        "renewal_info": renewal,
    }


def _check_optout(**kwargs: Any) -> str:
    """Tool body returns a JSON-encoded string for parity with the upstream."""
    npi = kwargs.get("npi")
    last_name = kwargs.get("last_name")
    if not npi and not last_name:
        return json.dumps({"error": "Either NPI or last name must be provided."})

    base = os.environ.get("CMS_API_BASE_URL", "").rstrip("/")
    dataset = os.environ.get("CMS_DATASET_ID", "opt-out-affidavits")
    if not base:
        return json.dumps({"error": "CMS_API_BASE_URL not configured"})

    url = f"{base}/{dataset}/data"
    params: dict[str, Any] = {}
    if npi:
        params["filter[NPI]"] = npi
    else:
        params["filter[filter-0][condition][path]"] = "Last Name"
        params["filter[filter-0][condition][value]"] = last_name
        if kwargs.get("first_name"):
            params["filter[filter-1][condition][path]"] = "First Name"
            params["filter[filter-1][condition][value]"] = kwargs["first_name"]
        if kwargs.get("state"):
            params["filter[filter-2][condition][path]"] = "State Code"
            params["filter[filter-2][condition][value]"] = kwargs["state"]

    try:
        status, resp, _ = request_json("GET", url, params=params)
    except Exception as exc:
        return json.dumps({"error": f"API request failed: {exc}"})

    if status >= 400:
        return json.dumps({"error": f"HTTP {status}: {resp.get('error') or 'unknown'}"})

    records = resp.get("records") or resp.get("items") or []
    if not records:
        return json.dumps({"message": "No opt-out record found", "records": []})
    formatted = [_format_record(r) for r in records]
    return json.dumps(formatted)


def build_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="medicare_optout_checker",
        description=(
            "Look up Medicare provider opt-out records in the CMS Provider "
            "Opt-Out Affidavits dataset by NPI or by last name (with "
            "optional state/specialty filters). Returns a JSON-encoded "
            "string for compatibility with the upstream tool."
        ),
        func=lambda **kwargs: _check_optout(**kwargs),
        args_schema=ProviderOptOutInput,
        handle_tool_error=True,
        handle_validation_error=True,
    )
