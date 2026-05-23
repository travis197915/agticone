"""Diagnosis coverage tool — slim repo-local port (``check_diagnosis_coverage``)."""
from __future__ import annotations

import os
from typing import Any

from langchain_core.tools import StructuredTool

from ._cache import ToolCache
from ._http import request_json
from ._logging import get_logger
from .schemas.diagnosis import DiagnosisInput

LOGGER = get_logger("diagnosis")
_CACHE = ToolCache(name="diagnosis", default_ttl=3600)


def _check_diagnosis(diagnosis_code: str) -> dict[str, Any]:
    code = (diagnosis_code or "").strip().upper()
    cached = _CACHE.get("lookup", code)
    if cached:
        return cached

    # Reuse CBD's OAuth token endpoint.
    from .cbd_tool import _token as cbd_token
    headers = {"Authorization": f"Bearer {cbd_token()}"}
    body = {
        "globalFilter": "",
        "filters": [{
            "id": "code",
            "value": [{"condition": "equals", "filterValue": code}],
        }],
    }
    try:
        status, resp, _ = request_json(
            "POST", os.environ["DIAGNOSIS_API_URL"], headers=headers, json=body,
        )
    except Exception as exc:
        return {
            "success": False,
            "diagnosis_code": code,
            "result": None,
            "error": str(exc),
        }

    if status >= 400:
        return {
            "success": False,
            "diagnosis_code": code,
            "result": None,
            "error": resp.get("error") or f"HTTP {status}",
        }

    rows = resp.get("rows") or resp.get("items") or []
    if not rows:
        out = {
            "success": True,
            "diagnosis_code": code,
            "result": {
                "diagnosis_code": code,
                "code_type": None,
                "covered": "No",
                "description": "Diagnosis code not found in coverage",
            },
            "error": None,
        }
        _CACHE.set("lookup", code, out)
        return out

    row = rows[0]
    types = ", ".join(
        t for t in [row.get("type1"), row.get("type2"), row.get("type3")]
        if t and t != "N/A"
    )
    recommendation = (row.get("coverageRecommend") or "").lower()
    out = {
        "success": True,
        "diagnosis_code": code,
        "result": {
            "diagnosis_code": code,
            "code_type": types or None,
            "covered": "Yes" if "cover services" in recommendation else "No",
            "description": row.get("description"),
        },
        "error": None,
    }
    _CACHE.set("lookup", code, out)
    return out


def build_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="check_diagnosis_coverage",
        description="Look up coverage info for an ICD-10 diagnosis via Covered Diagnosis API.",
        func=lambda diagnosis_code: _check_diagnosis(diagnosis_code),
        args_schema=DiagnosisInput,
    )
