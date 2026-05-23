"""
DOC360 claim read tool — slim repo-local port.

Public tool: ``doc360_read_claim_by_fln_dcc``.
"""
from __future__ import annotations

import os
import time
from typing import Any

from langchain_core.tools import StructuredTool

from ._cache import ToolCache
from ._http import request_json
from ._logging import get_logger
from .schemas.doc360 import ClaimReadByFlnInput

LOGGER = get_logger("doc360")
_CACHE = ToolCache(name="doc360_read_claim_by_fln_dcc", default_ttl=300)

_DOC360_TYPE_CHAIN = ("u_keyed_claim", "u_edi_claim", "u_clm_corsp_lwso_doc")


def _doc360_token() -> str:
    """Fetch a (mock) OAuth2 token. Cached briefly in process."""
    cached = _CACHE.get("token", "default")
    if cached:
        return cached
    url = os.environ["DOC360_TOKEN_URL"]
    body = {
        "client_id": os.environ.get("DOC360_CLIENT_ID", ""),
        "client_secret": os.environ.get("DOC360_CLIENT_SECRET", ""),
        "grant_type": "client_credentials",
    }
    _, resp, _ = request_json("POST", url, json=body)
    token = resp.get("access_token", "mock-token")
    _CACHE.set("token", "default", token, ttl=600)
    return token


def _doc360_read(fln_dcc: str) -> dict[str, Any]:
    """Try each DOC360 type in order; return the first one with content."""
    cached = _CACHE.get("read", fln_dcc)
    if cached:
        return cached

    base = os.environ["DOC360_API_BASE"]
    path = os.environ.get(
        "DOC360_READ_DOCUMENT_CONTENT",
        "/api/ecs/doc360-getcontent/v1/document-contents/read",
    )
    url = base.rstrip("/") + path
    token = _doc360_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Doc360-App-Id": os.environ.get("DOC360_APP_ID", "mock"),
        "Doc360-User-Id": os.environ.get("DOC360_USER_ID", "mock"),
        "Content-Type": "application/json",
    }

    last_status = 0
    tried: list[str] = []
    for type_name in _DOC360_TYPE_CHAIN:
        tried.append(type_name)
        body = {"lookupId": fln_dcc, "typeName": type_name}
        started = time.time()
        status, resp, resp_headers = request_json(
            "POST", url, headers=headers, json=body,
        )
        last_status = status
        if status == 401:
            _CACHE.set("token", "default", "", ttl=1)
            token = _doc360_token()
            headers["Authorization"] = f"Bearer {token}"
            status, resp, resp_headers = request_json(
                "POST", url, headers=headers, json=body,
            )
            last_status = status
        if status == 200 and resp.get("content") is not None:
            elapsed_ms = int((time.time() - started) * 1000)
            envelope = {
                "lookupId": fln_dcc,
                "status": "success",
                "httpStatus": status,
                "doc360TypeName": type_name,
                "content": resp.get("content"),
                "metadata": {
                    "responseTimeMs": elapsed_ms,
                    "contentType": resp_headers.get("Content-Type", ""),
                    "httpHeaders": resp_headers,
                },
            }
            _CACHE.set("read", fln_dcc, envelope)
            return envelope

    return {
        "lookupId": fln_dcc,
        "status": "error",
        "httpStatus": last_status,
        "doc360TypeName": None,
        "content": None,
        "metadata": {"responseTimeMs": None, "contentType": "", "httpHeaders": {}},
        "error": {
            "code": "NOT_FOUND",
            "message": f"DOC360 returned no content for FLN/DCC {fln_dcc}",
            "triedTypeNames": tried,
        },
    }


def _tool_fn(fln_dcc: str) -> dict[str, Any]:
    return _doc360_read((fln_dcc or "").strip())


def build_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="doc360_read_claim_by_fln_dcc",
        description=(
            "Read a claim 'print image' from DOC360 by FLN/DCC. Tries "
            "u_keyed_claim → u_edi_claim → u_clm_corsp_lwso_doc and returns "
            "the first envelope with content."
        ),
        func=_tool_fn,
        args_schema=ClaimReadByFlnInput,
    )
