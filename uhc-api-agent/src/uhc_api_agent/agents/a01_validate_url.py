"""URLValidatorAgent — sanitises the URL and method, assigns a call_id."""
from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from ..state import AgentState
    from ..config import AgentConfig

_VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def url_validator(state: "AgentState", cfg: "AgentConfig") -> dict:
    url = (state.get("url") or "").strip()
    if not url:
        return {
            "success": False,
            "error": "url is empty",
            "stages": [{"agent": "URLValidatorAgent", "status": "ERROR", "msg": "url is empty"}],
        }

    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    parsed = urlparse(url)
    if not parsed.netloc:
        return {
            "success": False,
            "error": f"invalid url: {url}",
            "stages": [{"agent": "URLValidatorAgent", "status": "ERROR", "msg": f"invalid url: {url}"}],
        }

    method = (state.get("method") or "GET").strip().upper()
    if method not in _VALID_METHODS:
        return {
            "success": False,
            "error": f"unsupported method: {method}",
            "stages": [{"agent": "URLValidatorAgent", "status": "ERROR", "msg": method}],
        }

    return {
        "url": url,
        "method": method,
        "call_id": state.get("call_id") or str(uuid.uuid4()),
        "stages": [{"agent": "URLValidatorAgent", "status": "OK", "msg": f"{method} {url}"}],
    }
