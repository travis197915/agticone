"""
Facets Claims Inquiry Tool (Individual Tools + ToolNode)

Four individual tools for each endpoint + ToolNode to combine them.

Benefits:
- Each endpoint is a separate tool
- Can be invoked individually or as a group via ToolNode
- Agent can choose which tools to use
- Modular and flexible architecture
"""

import json
import os
import re
import threading
import time
import warnings
from datetime import datetime
from typing import Any, Dict, Optional, List
from dataclasses import dataclass, asdict
import requests
import urllib3
from pathlib import Path
from dotenv import load_dotenv, dotenv_values
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolNode
from thynkr_bhagenticai.logging_utils import get_logger
from thynkr_bhagenticai.tool_cache import ToolCache
from urllib.parse import urlencode

# Import schemas
from tools.schemas.schema_facets_tool import ClaimNumberInput, ProviderDetailsInput

# Suppress warnings
warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = get_logger(__name__)
_CACHE = ToolCache()


def _find_env_file() -> Optional[str]:
    """Locate a local env file.

    Precedence:
        1) ENV_PATH (absolute path)
        2) repo root `.env`
        3) repo root `.env.stg` (fallback)
    """
    explicit = os.getenv("ENV_PATH")
    if explicit:
        return explicit

    repo_root = Path(__file__).resolve().parents[2]
    candidate_env = repo_root / ".env"
    if candidate_env.exists():
        return str(candidate_env)

    candidate_stg = repo_root / ".env.stg"
    if candidate_stg.exists():
        return str(candidate_stg)
    return None


_ENV_FILE = _find_env_file()

# Load env file if present, but support running purely from process env vars.
_ENV_VALUES: Dict[str, Optional[str]] = {}
if _ENV_FILE:
    load_dotenv(_ENV_FILE)
    _ENV_VALUES = dotenv_values(_ENV_FILE)


def _get_config_value(key: str, default: Optional[str] = None) -> Optional[str]:
    """Return configuration value, preferring process env over loaded env file."""
    return os.getenv(key) or _ENV_VALUES.get(key) or default


# ---------- Configuration ----------
FACETS_BASE_URL = (_get_config_value("FACETS_BASE_URL", "") or "").rstrip("/")
FACETS_USERNAME = _get_config_value("FACETS_USERNAME")
FACETS_PASSWORD = _get_config_value("FACETS_PASSWORD")
FACETS_REGION = _get_config_value("FACETS_REGION")
FACETS_IDENTITY = _get_config_value("FACETS_IDENTITY")
FACETS_SIGNON_METHOD = _get_config_value("FACETS_SIGNON_METHOD")

REQUEST_TIMEOUT = int(_get_config_value("REQUEST_TIMEOUT", "30") or "30")
_max_line_seq_raw = _get_config_value("MAX_LINE_SEQ", "100") or "100"
MAX_LINE_SEQ = int(_max_line_seq_raw) if str(_max_line_seq_raw).isdigit() else 100


# ==============================================================================
# HTTPResponse dataclass
# ==============================================================================

@dataclass
class HTTPResponse:
    """Container for HTTP response data."""
    status_code: Optional[int]
    body: Optional[Any]
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None/empty error field."""
        d = asdict(self)
        # Only include error field if it has a value (don't include None or empty strings)
        if not d.get("error"):
            d.pop("error", None)
        return d


# ==============================================================================
# HTTP helpers
# ==============================================================================

def _http_post(
    url: str,
    auth: Optional[tuple],
    json_body: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30
) -> HTTPResponse:
    """Make POST request with SSL verification control."""
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)

    verify = False if (_get_config_value("SSL_VERIFY", "false") or "false").lower() == "false" else True
    cert_path = _get_config_value("SSL_CERT_PATH")
    if cert_path and os.path.exists(cert_path):
        verify = cert_path

    try:
        resp = requests.post(url, json=json_body, auth=auth, headers=h, timeout=timeout, verify=verify)
        try:
            body = resp.json() if resp.content else None
        except (json.JSONDecodeError, ValueError):
            body = resp.text if resp.content else None
        # If error and no body, use HTTP reason phrase as message
        if (resp.status_code is not None) and (resp.status_code >= 400) and (not resp.content):
            body = resp.reason

        return HTTPResponse(status_code=resp.status_code, body=body)
    except Exception as e:
        return HTTPResponse(status_code=None, body=None, error=str(e))


def _http_get(url: str, headers: Dict[str, str], timeout: int = 30) -> HTTPResponse:
    """Make GET request with SSL verification control."""
    verify = False if (_get_config_value("SSL_VERIFY", "false") or "false").lower() == "false" else True
    cert_path = _get_config_value("SSL_CERT_PATH")
    if cert_path and os.path.exists(cert_path):
        verify = cert_path

    try:
        resp = requests.get(url, headers=headers, timeout=timeout, verify=verify)
        try:
            body = resp.json() if resp.content else None
        except (json.JSONDecodeError, ValueError):
            body = resp.text if resp.content else None
        # If error and no body, use HTTP reason phrase as message
        if (resp.status_code is not None) and (resp.status_code >= 400) and (not resp.content):
            body = resp.reason

        return HTTPResponse(status_code=resp.status_code, body=body)
    except Exception as e:
        return HTTPResponse(status_code=None, body=None, error=str(e))


def _extract_token(token_resp: HTTPResponse) -> Optional[str]:
    """Extract access token from response."""
    if token_resp.body is None:
        return None

    if isinstance(token_resp.body, dict):
        data = token_resp.body.get("Data", {})
        if isinstance(data, dict):
            token = data.get("access_token")
            if token:
                return token

        for key in ("AccessToken", "access_token", "Token", "token", "accessToken"):
            val = token_resp.body.get(key)
            if isinstance(val, str) and val:
                return val

    if isinstance(token_resp.body, str) and token_resp.body and token_resp.status_code and 200 <= token_resp.status_code < 300:
        return token_resp.body.strip()

    return None


# ==============================================================================
# Config validation helpers
# ==============================================================================

def _validate_facets_base_url(url: str) -> Optional[str]:
    """Return error message if base URL is invalid, else None."""
    if not url:
        return "FACETS_BASE_URL not configured"
    lowered = url.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return "FACETS_BASE_URL must start with http:// or https://"
    return None


def _validate_required_env() -> List[str]:
    """Return a list of missing required environment keys for token acquisition."""
    missing: List[str] = []
    if not FACETS_USERNAME:
        missing.append("FACETS_USERNAME")
    if not FACETS_PASSWORD:
        missing.append("FACETS_PASSWORD")
    if not FACETS_REGION:
        missing.append("FACETS_REGION")
    if not FACETS_IDENTITY:
        missing.append("FACETS_IDENTITY")
    if not FACETS_SIGNON_METHOD:
        missing.append("FACETS_SIGNON_METHOD")
    return missing


# ==============================================================================
# Token Management
# ==============================================================================

# Facets token TTL cache - avoids re-fetching on every tool call
_facets_token: Optional[str] = None
_facets_token_status: Optional[Dict[str, Any]] = None
_facets_token_expiry: float = 0.0            # monotonic clock
_facets_token_lock = threading.Lock()
_FACETS_TOKEN_TTL_SECONDS = 50 * 60         # refresh 10 min before typical 60-min expiry


def get_facets_token() -> tuple[Optional[str], Dict[str, Any]]:
    """
    Get authentication token from Facets API.

    Returns a cached token when the TTL has not expired, otherwise
    fetches a fresh one from the Facets security endpoint.

    Returns:
        tuple: (token_string, status_dict)
            - token_string: The actual Bearer token (or None if failed)
            - status_dict: Sanitized status info (doesn't include token)
    """
    global _facets_token, _facets_token_status, _facets_token_expiry

    # Fast path - token is still valid
    if _facets_token and time.monotonic() < _facets_token_expiry:
        return _facets_token, _facets_token_status  # type: ignore[return-value]

    with _facets_token_lock:
        # Double-check after acquiring the lock
        if _facets_token and time.monotonic() < _facets_token_expiry:
            return _facets_token, _facets_token_status  # type: ignore[return-value]

        token, status = _fetch_facets_token()
        if token:
            _facets_token = token
            _facets_token_status = status
            _facets_token_expiry = time.monotonic() + _FACETS_TOKEN_TTL_SECONDS
            logger.info("facets_token_refreshed", extra={"ttl_seconds": _FACETS_TOKEN_TTL_SECONDS})
        return token, status


def _fetch_facets_token() -> tuple[Optional[str], Dict[str, Any]]:
    """Actually fetch a new Facets token from the API."""
    # Validate base URL early to avoid invalid relative URL errors
    base_url_error = _validate_facets_base_url(FACETS_BASE_URL)
    if base_url_error:
        status = {
            "status": "failed",
            "status_code": None,
            "error": base_url_error,
            "message": f"{base_url_error}; set it in environment or .env",
        }
        logger.warning(
            base_url_error,
            extra={"endpoint": "security/tokens"},
        )
        return None, status

    # Validate required environment for token acquisition
    missing_keys = _validate_required_env()
    if missing_keys:
        status = {
            "status": "failed",
            "status_code": None,
            "error": "Missing required FACETS configuration",
            "missing": missing_keys,
            "message": f"Missing configuration keys: {', '.join(missing_keys)}",
        }
        logger.warning(
            "Missing FACETS configuration keys",
            extra={"missing": missing_keys, "endpoint": "security/tokens"},
        )
        return None, status

    token_url = f"{FACETS_BASE_URL}/security/tokens"

    logger.info("Requesting Facets token", extra={"endpoint": "security/tokens"})

    token_resp = _http_post(
        url=token_url,
        auth=(FACETS_USERNAME, FACETS_PASSWORD) if FACETS_USERNAME and FACETS_PASSWORD else None,
        json_body={
            "Region": FACETS_REGION,
            "FacetsIdentity": FACETS_IDENTITY,
            "SignonMethod": FACETS_SIGNON_METHOD
        },
        timeout=REQUEST_TIMEOUT,
    )

    token = _extract_token(token_resp)

    # Sanitized status (no token exposed)
    status = {
        "status_code": token_resp.status_code,
        "error": token_resp.error
    }

    if token_resp.status_code and 200 <= token_resp.status_code < 300:
        status["status"] = "success"
        status["message"] = "Token acquired successfully"
    else:
        status["status"] = "failed"
        status["message"] = f"Token acquisition failed: {token_resp.error or token_resp.status_code}"

    logger.info(
        "Facets token response",
        extra={"status": status.get("status"), "status_code": token_resp.status_code},
    )

    return token, status


# ==============================================================================
# Error helpers
# ==============================================================================

def _http_error_message(resp: Optional[HTTPResponse]) -> str:
    """Best-effort extraction of a human-readable HTTP error message.

    Priority:
    - resp.error
    - common keys in resp.body if dict (message/error/detail)
    - resp.body if str
    - HTTP <status_code>
    - Unknown error
    """
    if resp is None:
        return "Unknown error"
    if resp.error:
        return str(resp.error)
    body = resp.body
    if isinstance(body, dict):
        for k in ("message", "Message", "error", "Error", "detail", "Detail"):
            v = body.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    if isinstance(body, str) and body.strip():
        return body.strip()
    if resp.status_code is not None:
        return f"HTTP {resp.status_code}"
    return "Unknown error"


def _extract_status_meta(resp: Optional[HTTPResponse]) -> Dict[str, Optional[str]]:
    """Extract status_code and a status_message (if available) from response.

    Tries common locations:
    - body["Status"]["StatusMessage"]
    - body["StatusMessage"]
    - body string
    - HTTP reason via _http_error_message fallback
    """
    code = resp.status_code if resp and (resp.status_code is not None) else None
    msg: Optional[str] = None
    if resp and isinstance(resp.body, dict):
        status_obj = resp.body.get("Status")
        if isinstance(status_obj, dict):
            sm = status_obj.get("StatusMessage")
            if isinstance(sm, str) and sm.strip():
                msg = sm.strip()
        if not msg:
            sm2 = resp.body.get("StatusMessage")
            if isinstance(sm2, str) and sm2.strip():
                msg = sm2.strip()
    if not msg and resp and isinstance(resp.body, str) and resp.body.strip():
        msg = resp.body.strip()
    if not msg:
        msg = _http_error_message(resp)
    return {"status_code": code, "status_message": msg}


def _find_first_key(obj: Any, target_key: str) -> Optional[Any]:
    """Depth-first search for the first occurrence of target_key in nested dict/list."""
    if isinstance(obj, dict):
        if target_key in obj:
            return obj.get(target_key)
        for v in obj.values():
            found = _find_first_key(v, target_key)
            if found is not None:
                return found
        return None
    if isinstance(obj, list):
        for item in obj:
            found = _find_first_key(item, target_key)
            if found is not None:
                return found
        return None
    return None


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Parse common ISO-like datetime strings returned by Facets."""
    if not isinstance(value, str) or not value.strip():
        return None

    raw = value.strip()
    try:
        # Handles values like 2025-01-24T00:00:00 and 2025-08-14T00:17:29.823
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_line_date_ranges(summary_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract all line-level date ranges (CDML_SEQ_NO, CDML_FROM_DT, CDML_TO_DT) from summary payload.

    Returns list of dicts with structure:
    {
        "CDML_SEQ_NO": line_seq_number,
        "CDML_FROM_DT": datetime_str,
        "CDML_TO_DT": datetime_str
    }
    """
    ranges: List[Dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            # Only extract if all three fields are present
            cdml_seq_no = node.get("CDML_SEQ_NO")
            cdml_from_dt = node.get("CDML_FROM_DT")
            cdml_to_dt = node.get("CDML_TO_DT")
            if (cdml_seq_no is not None and isinstance(cdml_from_dt, str) and isinstance(cdml_to_dt, str)):
                ranges.append({
                    "CDML_SEQ_NO": cdml_seq_no,
                    "CDML_FROM_DT": cdml_from_dt,
                    "CDML_TO_DT": cdml_to_dt
                })
            for child in node.values():
                _walk(child)
        elif isinstance(node, list):
            for child in node:
                _walk(child)

    _walk(summary_payload)
    return ranges


def _extract_duplicate_claim_results(search_body: Any) -> List[Dict[str, Any]]:
    """Extract duplicate claim rows from known response shapes."""
    if not isinstance(search_body, dict):
        return []

    # Shape seen in Insomnia:
    # {"Data": {"Results": {"CIV7_COLL": [ ... ]}}}
    data = search_body.get("Data")
    if isinstance(data, dict):
        results = data.get("Results")
        if isinstance(results, dict):
            civ7_coll = results.get("CIV7_COLL")
            if isinstance(civ7_coll, list):
                return [row for row in civ7_coll if isinstance(row, dict)]

    # Fallback shape:
    # {"Claims": [ ... ]}
    claims = search_body.get("Claims")
    if isinstance(claims, list):
        return [row for row in claims if isinstance(row, dict)]

    return []


# ==============================================================================
# Individual Tool Functions (5 Functions)
# ==============================================================================

def get_claim_summary(claim_number: str) -> Dict[str, Any]:
    """
    Tool 1: Fetch summarize data for each claim number from Facets API.

    Args:
        claim_number: Claim ID to query (e.g., "25XG44660400")

    Returns:
        dict: Response with status_code, body, error, and metadata
    """
    # Normalize and check cache
    claim_number = str(claim_number).strip()
    cached = _CACHE.get("facets_get_summary", {"claim_number": claim_number})
    if cached.hit and isinstance(cached.value, dict):
        logger.info("Cache hit", extra={"endpoint": "summary"})
        return cached.value

    # Get token
    token, token_status = get_facets_token()
    if not token:
        logger.warning("Token not generated", extra={"endpoint": "summary"})
        payload = {
            "error": token_status.get("message") or token_status.get("error") or "Token error",
            "message": token_status.get("message") or token_status.get("error") or "Token error",
            "claim_number": claim_number,
            "endpoint": "summary",
            "timestamp": int(time.time()),
            "token_status": token_status,
        }
        return payload

    # Build headers
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

    # Make request
    summary_url = f"{FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/Summary"
    logger.info("Calling Facets claim summary", extra={"endpoint": "summary"})
    summary_resp = _http_get(summary_url, headers=headers, timeout=REQUEST_TIMEOUT)

    # Error handling for bad claim numbers / failed fetch
    if summary_resp.status_code is None or (summary_resp.status_code >= 400):
        meta = _extract_status_meta(summary_resp)
        msg = meta.get("status_message")
        payload = {
            "error": msg,
            "message": msg,
            "status_code": meta.get("status_code"),
            "status_message": meta.get("status_message"),
            "claim_number": claim_number,
            "endpoint": "summary",
            "timestamp": int(time.time()),
        }
        return payload

    # Build response
    result = summary_resp.to_dict()
    result["claim_number"] = claim_number
    result["endpoint"] = "summary"
    result["timestamp"] = int(time.time())
    meta = _extract_status_meta(summary_resp)
    result["status_code"] = meta.get("status_code")
    result["status_message"] = meta.get("status_message")

    # Field mapping transformation removed

    logger.info(
        "Facets claim summary response",
        extra={"endpoint": "summary", "status_code": result.get("status_code")},
    )

    # Cache successful responses only
    _CACHE.set("facets_get_summary", {"claim_number": claim_number}, result)
    return result


# def get_claim_providers(claim_number: str) -> Dict[str, Any]:
#     """
#     Tool 2: Fetch provider information related to a claim number from Facets API.
#
#     Args:
#         claim_number: Claim ID to query (e.g., "25XG44660400")
#
#     Returns:
#         dict: Response with status_code, body, error, and metadata
#     """
#     # Normalize and check cache
#     claim_number = str(claim_number).strip()
#     cached = _CACHE.get("facets_get_providers", {"claim_number": claim_number})
#     if cached.hit and isinstance(cached.value, dict):
#         logger.info("Cache hit", extra={"endpoint": "providers"})
#         return cached.value
#
#     # Get token
#     token, token_status = get_facets_token()
#     if not token:
#         payload = {
#             "error": token_status.get("message") or token_status.get("error") or "Token error",
#             "message": token_status.get("message") or token_status.get("error") or "Token error",
#             "claim_number": claim_number,
#             "endpoint": "providers",
#             "timestamp": int(time.time()),
#             "token_status": token_status,
#         }
#         return payload
#
#     # Build headers
#     headers = {
#         "Accept": "application/json",
#         "Content-Type": "application/json",
#         "Authorization": f"Bearer {token}"
#     }
#
#     # Make request
#     providers_url = f"{FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/Providers"
#     logger.info("Calling Facets claim providers", extra={"endpoint": "providers"})
#     providers_resp = _http_get(providers_url, headers=headers, timeout=REQUEST_TIMEOUT)
#
#     if providers_resp.status_code is None or (providers_resp.status_code >= 400):
#         meta = _extract_status_meta(providers_resp)
#         msg = meta.get("status_message")
#         payload = {
#             "error": msg,
#             "message": msg,
#             "status_code": meta.get("status_code"),
#             "status_message": meta.get("status_message"),
#             "claim_number": claim_number,
#             "endpoint": "providers",
#             "timestamp": int(time.time()),
#         }
#         return payload
#
#     # Build response
#     result = providers_resp.to_dict()
#     result["claim_number"] = claim_number
#     result["endpoint"] = "providers"
#     result["timestamp"] = int(time.time())
#     meta = _extract_status_meta(providers_resp)
#     result["status_code"] = meta.get("status_code")
#     result["status_message"] = meta.get("status_message")
#
#     # Field mapping transformation removed
#
#     logger.info(
#         "Facets claim providers response",
#         extra={"endpoint": "providers", "status_code": result.get("status_code")},
#     )
#
#     _CACHE.set("facets_get_providers", {"claim_number": claim_number}, result)
#     return result


def get_claim_cob(claim_number: str) -> Dict[str, Any]:
    """
    Tool 3: Fetch claim Coordination of Benefits (COB) data(if present) related to a particular claim numberfrom Facets API.

    Args:
        claim_number: Claim ID to query (e.g., "25XG44660400")

    Returns:
        dict: Response with status_code, body, error, and metadata
    """
    # Normalize and check cache
    claim_number = str(claim_number).strip()
    cached = _CACHE.get("facets_get_cob", {"claim_number": claim_number})
    if cached.hit and isinstance(cached.value, dict):
        logger.info("Cache hit", extra={"endpoint": "cob"})
        return cached.value

    # Get token
    token, token_status = get_facets_token()
    if not token:
        payload = {
            "error": token_status.get("message") or token_status.get("error") or "Token error",
            "message": token_status.get("message") or token_status.get("error") or "Token error",
            "claim_number": claim_number,
            "endpoint": "cob",
            "timestamp": int(time.time()),
            "token_status": token_status,
        }
        return payload

    # Build headers
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

    # Make request
    cob_url = f"{FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/COB"
    logger.info("Calling Facets claim cob", extra={"endpoint": "cob"})
    cob_resp = _http_get(cob_url, headers=headers, timeout=REQUEST_TIMEOUT)

    if cob_resp.status_code is None or (cob_resp.status_code >= 400):
        meta = _extract_status_meta(cob_resp)
        msg = meta.get("status_message")
        payload = {
            "error": msg,
            "message": msg,
            "status_code": meta.get("status_code"),
            "status_message": meta.get("status_message"),
            "claim_number": claim_number,
            "endpoint": "cob",
            "timestamp": int(time.time()),
        }
        return payload

    # Build response
    result = cob_resp.to_dict()
    result["claim_number"] = claim_number
    result["endpoint"] = "cob"
    result["timestamp"] = int(time.time())
    meta = _extract_status_meta(cob_resp)
    result["status_code"] = meta.get("status_code")
    result["status_message"] = meta.get("status_message")

    # Field mapping transformation removed

    logger.info(
        "facets claim cob response",
        extra={"endpoint": "cob", "status_code": result.get("status_code")},
    )

    _CACHE.set("facets_get_cob", {"claim_number": claim_number}, result)
    return result


def get_claim_line_details(claim_number: str) -> Dict[str, Any]:
    """
    Tool 4: Fetch all claim line details information from Facets API for a particular claim number(iterates until 404 to get first missing line details).

    Args:
        claim_number: Claim ID to query (e.g., "25XG44660400")

    Returns:
        dict: Response with status_code, body, error, and metadata
    """
    # Normalize and check cache
    claim_number = str(claim_number).strip()
    cached = _CACHE.get("facets_get_line_details", {"claim_number": claim_number})
    if cached.hit and isinstance(cached.value, dict):
        logger.info("Cache hit", extra={"endpoint": "line_details"})
        return cached.value

    # Get token
    token, token_status = get_facets_token()
    if not token:
        payload = {
            "error": token_status.get("message") or token_status.get("error") or "Token error",
            "message": token_status.get("message") or token_status.get("error") or "Token error",
            "claim_number": claim_number,
            "endpoint": "line_details",
            "timestamp": int(time.time()),
            "token_status": token_status,
        }
        return payload

    # Build headers
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

    # Iterate through line sequences
    line_items = []
    first_missing_seq: Optional[int] = None
    seq = 1
    logger.info("Calling Facets claim line_details", extra={"endpoint": "line_details"})

    while True:
        if seq > MAX_LINE_SEQ:
            break

        line_url = f"{FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/Lines/{seq}/Details"
        line_resp = _http_get(line_url, headers=headers, timeout=REQUEST_TIMEOUT)
        status = line_resp.status_code

        if status == 404:
            first_missing_seq = seq
            break
        elif status is None:
            line_items.append({"line_seq": seq, **line_resp.to_dict()})
            first_missing_seq = seq
            break
        else:
            line_items.append({"line_seq": seq, **line_resp.to_dict()})
            seq += 1

    # Build result
    result = {
        "claim_number": claim_number,
        "endpoint": "line_details",
        "timestamp": int(time.time()),
        "first_missing_line_seq": first_missing_seq,
        "total_lines": len(line_items),
        "items": line_items
    }

    # Field mapping transformation removed

    logger.info(
        "facets claim line_details response",
        extra={
            "endpoint": "line_details",
            "total_lines": result.get("total_lines"),
            "first_missing_line_seq": result.get("first_missing_line_seq"),
        },
    )

    _CACHE.set("facets_get_line_details", {"claim_number": claim_number}, result)
    return result


def get_member_eligibility(claim_number: str) -> Dict[str, Any]:
    """
    Tool: Fetch member eligibility information from Facets API using MEME_CK from claim summary.
    This function will first hit claim summary function then from there extract MEME_CK to call member eligibility endpoint.
    Args:
        claim_number: Claim ID to query (e.g., "25XG44660400")
    Returns:
        dict: Response with status_code, body, error, and metadata
    """
    claim_number = str(claim_number).strip()
    cached = _CACHE.get("facets_get_member_eligibility", {"claim_number": claim_number})
    if cached.hit and isinstance(cached.value, dict):
        logger.info("Cache hit", extra={"endpoint": "member_eligibility"})
        return cached.value

    # Get token
    token, token_status = get_facets_token()
    if not token:
        payload = {
            "error": token_status.get("message") or token_status.get("error") or "Token error",
            "message": token_status.get("message") or token_status.get("error") or "Token error",
            "claim_number": claim_number,
            "endpoint": "member_eligibility",
            "timestamp": int(time.time()),
            "token_status": token_status,
        }
        return payload

    # Build headers
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    # Step 1: Get MEME_CK from summary
    summary = get_claim_summary(claim_number)
    meme_ck = _find_first_key(summary, "MEME_CK")
    if meme_ck is None:
        logger.error("Could not extract MEME_CK", extra={"endpoint": "member_eligibility"})
        return {
            "error": "Could not extract MEME_CK",
            "claim_number": claim_number,
            "endpoint": "member_eligibility"
        }

    # Make request
    member_eligibility_url = f"{FACETS_BASE_URL}/Members/Coverage/MemberKey/{meme_ck}/Eligibility"
    logger.info("Calling Facets member eligibility", extra={"endpoint": "member_eligibility"})
    member_eligibility_resp = _http_get(member_eligibility_url, headers=headers, timeout=REQUEST_TIMEOUT)

    if member_eligibility_resp.status_code is None or (member_eligibility_resp.status_code >= 400):
        meta = _extract_status_meta(member_eligibility_resp)
        msg = meta.get("status_message")
        payload = {
            "error": msg,
            "message": msg,
            "status_code": meta.get("status_code"),
            "status_message": meta.get("status_message"),
            "claim_number": claim_number,
            "endpoint": "member_eligibility_resp",
            "timestamp": int(time.time()),
        }
        return payload

    # Build response
    result = member_eligibility_resp.to_dict()
    result["claim_number"] = claim_number
    result["endpoint"] = "member_eligibility"
    result["timestamp"] = int(time.time())
    meta = _extract_status_meta(member_eligibility_resp)
    result["status_code"] = meta.get("status_code")
    result["status_message"] = meta.get("status_message")

    # Field mapping transformation removed

    logger.info(
        "Facets claim member eligibility response",
        extra={"endpoint": "member_eligibility", "status_code": result.get("status_code")},
    )

    _CACHE.set("facets_get_member_eligibility", {"claim_number": claim_number}, result)
    return result


def _normalize_optional_str(value: Optional[str]) -> str:
    """Normalize optional string-ish values to empty string when unset."""
    if value is None:
        return ""
    normalized = str(value).strip()
    if normalized.lower() in {"none", "null", "nan"}:
        return ""
    return normalized


def _extract_scalar_value(candidate: Any) -> str:
    """Extract a scalar string from nested parser envelopes/lists."""
    if candidate is None:
        return ""

    if isinstance(candidate, dict):
        # Common parser envelope shape: {"value": ..., "confidence": ..., "provenance": ...}
        for key in ("value", "Value", "tin", "tax_id", "federal_tax_id"):
            if key in candidate:
                return _extract_scalar_value(candidate.get(key))
        return ""

    if isinstance(candidate, (list, tuple)):
        for item in candidate:
            resolved = _extract_scalar_value(item)
            if resolved:
                return resolved
        return ""

    return _normalize_optional_str(str(candidate))


def _normalize_tax_id(candidate: Any) -> str:
    """Normalize tax id/TIN values (handles dict envelopes and punctuation)."""
    raw = _extract_scalar_value(candidate)
    if not raw:
        return ""

    digits = "".join(ch for ch in raw if ch.isdigit())
    # EIN/TIN is typically 9 digits; when present, prefer pure digits.
    if len(digits) >= 9:
        return digits
    return raw


def _normalize_npi(candidate: Any) -> str:
    """Normalize NPI values to digit-only representation."""
    raw = _extract_scalar_value(candidate)
    if not raw:
        return ""
    return "".join(ch for ch in raw if ch.isdigit())


def _resolve_provider_details_inputs_from_claim(
    claim_number_for_reference: str,
) -> Dict[str, str]:
    """Resolve provider_entity_type from Facets claim summary."""
    resolved_provider_entity_type = ""

    summary = get_claim_summary(claim_number_for_reference)
    if not isinstance(summary, dict) or summary.get("error"):
        return {
            "provider_entity_type": resolved_provider_entity_type,
        }

    provider_entity_candidates = [
        _find_first_key(summary, "PRPR_ENTITY"),
        _find_first_key(summary, "Provider Entity Type"),
        _find_first_key(summary, "provider_entity_type"),
    ]
    for candidate in provider_entity_candidates:
        normalized = _normalize_optional_str(candidate).upper()
        if normalized:
            resolved_provider_entity_type = normalized
            break

    return {
        "provider_entity_type": resolved_provider_entity_type,
    }


def _resolve_doc360_parsed_payload(claim_number_for_reference: str) -> Dict[str, Any]:
    """Resolve DOC360 parsed payload for a claim number."""
    # Circular-dependency exception: lazy import avoids module cycle
    # (claim_micro_image_id_flndcc imports get_claim_summary from this module).
    from tools.claim_micro_image_id_to_fln_dcc_doc360_parse import claim_micro_image_id_to_fln_dcc_doc360_parse

    if hasattr(claim_micro_image_id_to_fln_dcc_doc360_parse, "invoke"):
        result = claim_micro_image_id_to_fln_dcc_doc360_parse.invoke(
            {"claim_number": claim_number_for_reference}
        )
    else:
        result = claim_micro_image_id_to_fln_dcc_doc360_parse(claim_number_for_reference)

    if hasattr(result, "model_dump"):
        doc360_payload: Dict[str, Any] = result.model_dump()
    elif isinstance(result, dict):
        doc360_payload = result
    else:
        return {}

    if doc360_payload.get("status") == "error":
        return {}

    parsed = doc360_payload.get("parsed")
    return parsed if isinstance(parsed, dict) else {}


def _resolve_tax_id_from_doc360_parsed(parsed: Dict[str, Any]) -> str:
    """Resolve tax_id from DOC360 fields containing `25 FEDERAL TAX ID`.

    Supports exact and prefixed keys (for example:
    `25 FEDERAL TAX ID#`, `totals_25_30A.25 FEDERAL TAX ID#`).
    """
    fields = parsed.get("fields") if isinstance(parsed.get("fields"), dict) else {}

    # Prefer exact canonical key when available.
    exact_tax_id = _normalize_tax_id(fields.get("25 FEDERAL TAX ID#"))
    if exact_tax_id:
        return exact_tax_id

    # Fallback: accept any key that contains "25 FEDERAL TAX ID".
    target = "25 federal tax id"
    for key, value in fields.items():
        if target in str(key).lower():
            resolved = _normalize_tax_id(value)
            if resolved:
                return resolved

    return ""


def _resolve_rendering_npi_from_doc360_parsed(parsed: Dict[str, Any]) -> List[str]:
    """Resolve all distinct NPIs from parsed DOC360 payload.

    Collects NPIs from any nested key containing the substring "npi"
    (case-insensitive), including `fields` and nested structures like
    `line_items[].rendering_npi` and `line_items[].svc_npi`.
    """
    seen: set[str] = set()
    ordered_npis: List[str] = []

    def _add_if_valid(candidate: Any) -> None:
        normalized = _normalize_npi(candidate)
        if len(normalized) == 10 and normalized not in seen:
            seen.add(normalized)
            ordered_npis.append(normalized)

    def _scan(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_l = str(key).lower()

                # Primary rule: any field/key containing "npi".
                if "npi" in key_l:
                    _add_if_valid(value)

                # Secondary rule: occasionally values themselves contain
                # text like "NPI 1234567890".
                if isinstance(value, str) and "npi" in value.lower():
                    for match in re.findall(r"\b\d{10}\b", value):
                        _add_if_valid(match)

                if isinstance(value, (dict, list)):
                    _scan(value)
            return

        if isinstance(obj, list):
            for item in obj:
                _scan(item)

    _scan(parsed)
    return ordered_npis


def get_provider_details(
    claim_number_for_reference: str,
    provider_entity_type: Optional[str] = None,
    tax_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Tool: Fetch provider details from Facets API using PRPR_ENTITY and MCTN_ID.
    Calls the procedure execute endpoint with the stored procedure

    Args:
        provider_entity_type: Provider entity type (maps to PRPR_ENTITY; allowed: P, G, I, F)
        tax_id: Tax ID from DOC360 field `25 FEDERAL TAX ID#`
        provider_entity_type will be found at facets_summary_tool output as name PRPR_ENTITY
        tax_id will be found at doc360 tool output field `25 FEDERAL TAX ID#`

    Returns:
        dict: Response with status_code, body, error, and metadata
    """
    provider_entity_type = _normalize_optional_str(provider_entity_type).upper()
    tax_id = _normalize_optional_str(tax_id)
    claim_number_for_reference = _normalize_optional_str(claim_number_for_reference)
    doc360_rendering_npis: List[str] = []
    cache_filter_version = "npi_filter_v4"

    if not claim_number_for_reference:
        logger.error("Missing claim_number_for_reference input", extra={"endpoint": "provider_details"})
        return {
            "error": "claim_number_for_reference is required",
            "provider_entity_type": provider_entity_type,
            "tax_id": tax_id,
            "claim_number_for_reference": claim_number_for_reference,
            "endpoint": "provider_details",
        }

    resolved = _resolve_provider_details_inputs_from_claim(claim_number_for_reference)
    provider_entity_type = resolved.get("provider_entity_type", "")
    parsed = _resolve_doc360_parsed_payload(claim_number_for_reference)
    tax_id = _resolve_tax_id_from_doc360_parsed(parsed)
    doc360_rendering_npis = _resolve_rendering_npi_from_doc360_parsed(parsed)

    allowed_provider_entity_types = {"G", "P", "F", "I"}
    cached = _CACHE.get(
        "facets_get_provider_details",
        {
            "provider_entity_type": provider_entity_type,
            "tax_id": tax_id,
            "claim_number_for_reference": claim_number_for_reference,
            "doc360_rendering_npis": doc360_rendering_npis,
            "filter_version": cache_filter_version,
        },
    )
    if cached.hit and isinstance(cached.value, dict):
        logger.info("Cache hit", extra={"endpoint": "provider_details"})
        return cached.value

    # Get token
    token, token_status = get_facets_token()
    if not token:
        payload = {
            "error": token_status.get("message") or token_status.get("error") or "Token error",
            "message": token_status.get("message") or token_status.get("error") or "Token error",
            "provider_entity_type": provider_entity_type,
            "tax_id": tax_id,
            "claim_number_for_reference": claim_number_for_reference,
            "endpoint": "provider_details",
            "timestamp": int(time.time()),
            "token_status": token_status,
        }
        return payload

    if not provider_entity_type:
        logger.error("Missing PRPR_ENTITY input", extra={"endpoint": "provider_details"})
        return {
            "error": "provider_entity_type (PRPR_ENTITY) is required",
            "provider_entity_type": provider_entity_type,
            "tax_id": tax_id,
            "claim_number_for_reference": claim_number_for_reference,
            "endpoint": "provider_details",
        }

    if provider_entity_type not in allowed_provider_entity_types:
        logger.error("Invalid PRPR_ENTITY input", extra={"endpoint": "provider_details"})
        return {
            "error": (
                "provider_entity_type (PRPR_ENTITY) must be one of: "
                "P=Practitioner, G=Provider Group, I=IPA, F=Facility"
            ),
            "provider_entity_type": provider_entity_type,
            "tax_id": tax_id,
            "claim_number_for_reference": claim_number_for_reference,
            "endpoint": "provider_details",
        }

    if not tax_id:
        logger.error("Missing MCTN_ID input", extra={"endpoint": "provider_details"})
        return {
            "error": "tax_id (MCTN_ID) is required",
            "provider_entity_type": provider_entity_type,
            "tax_id": tax_id,
            "claim_number_for_reference": claim_number_for_reference,
            "endpoint": "provider_details",
        }

    # Build headers with Authorization (no basic auth; token used in header)
    headers = {
        "Authorization": f"Bearer {token}"
    }

    # Make POST request to procedure execute endpoint
    procedure_url = f"{FACETS_BASE_URL}/data/procedure/execute"
    json_body = {
        "Procedure": "CMCSP_PRV1_SRCH_PRPR_NAME_REMT",
        "Parameters": {
            "PRPR_ENTITY": provider_entity_type,
            "PRPR_NAME": "%",
            "PRAD_CITY": "%",
            "PRAD_STATE": "%",
            "PRAD_ZIP": "%",
            "MCTN_ID": tax_id,
        },
    }

    logger.info("Calling Facets provider details", extra={"endpoint": "provider_details"})
    provider_resp = _http_post(
        url=procedure_url,
        auth=None,
        json_body=json_body,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )

    if provider_resp.status_code is None or (provider_resp.status_code >= 400):
        meta = _extract_status_meta(provider_resp)
        msg = meta.get("status_message")
        payload = {
            "error": msg,
            "message": msg,
            "status_code": meta.get("status_code"),
            "status_message": meta.get("status_message"),
            "provider_entity_type": provider_entity_type,
            "tax_id": tax_id,
            "claim_number_for_reference": claim_number_for_reference,
            "endpoint": "provider_details",
            "timestamp": int(time.time()),
        }
        return payload

    # Build response
    result = provider_resp.to_dict()
    result["provider_entity_type"] = provider_entity_type
    result["tax_id"] = tax_id
    result["claim_number_for_reference"] = claim_number_for_reference
    result["doc360_rendering_npis"] = doc360_rendering_npis
    result["endpoint"] = "provider_details"
    result["timestamp"] = int(time.time())
    meta = _extract_status_meta(provider_resp)
    result["status_code"] = meta.get("status_code")
    result["status_message"] = meta.get("status_message")

    # Filter provider rows by PRPR_NPI using DOC360 NPI fields (24, 33, 11).
    # Keep only rows where PRPR_NPI matches ANY of the extracted NPIs.
    rows_before_filter = 0
    rows_after_filter = 0
    filter_applied = False
    filter_skipped_reason = ""
    if doc360_rendering_npis:
        body = result.get("body")
        if isinstance(body, dict):
            data = body.get("Data")
            if isinstance(data, dict):
                result_sets = data.get("ResultSets")
                if isinstance(result_sets, list):
                    for result_set in result_sets:
                        if not isinstance(result_set, dict):
                            continue
                        rows = result_set.get("Rows")
                        if not isinstance(rows, list):
                            continue

                        filter_applied = True
                        rows_before_filter += len(rows)
                        # Keep rows where PRPR_NPI matches ANY of the extracted NPIs
                        filtered_rows = [
                            row
                            for row in rows
                            if isinstance(row, dict)
                            and _normalize_npi(row.get("PRPR_NPI")) in doc360_rendering_npis
                        ]
                        result_set["Rows"] = filtered_rows
                        result_set["RowCount"] = len(filtered_rows)
                        rows_after_filter += len(filtered_rows)

                    if filter_applied:
                        data["TotalRowCount"] = rows_after_filter
    else:
        filter_skipped_reason = "DOC360 NPI fields (24 CONTINUED RENDERING NPI, 33 NPI, 11 NPI) all missing or empty; returning unfiltered rows"

    result["filter_applied"] = filter_applied
    result["rows_before_filter"] = rows_before_filter
    result["rows_after_filter"] = rows_after_filter
    result["filter_skipped_reason"] = filter_skipped_reason

    logger.info(
        "Facets provider details response",
        extra={"endpoint": "provider_details", "status_code": result.get("status_code")},
    )

    _CACHE.set(
        "facets_get_provider_details",
        {
            "provider_entity_type": provider_entity_type,
            "tax_id": tax_id,
            "claim_number_for_reference": claim_number_for_reference,
            "doc360_rendering_npis": doc360_rendering_npis,
            "filter_version": cache_filter_version,
        },
        result,
    )
    return result


def get_duplicate_claim(claim_number: str) -> Dict[str, Any]:
    """
    Tool: Search for duplicate claims using claim summary data and filter by service date ranges.
    This function calls get_claim_summary to extract required parameters, then searches for
    potential duplicate claims and filters them based on line item service date ranges.

    Args:
        claim_number: Claim ID to query (e.g., "25XG44660400")

    Returns:
        dict: Response with filtered duplicate claims matching service date criteria
    """
    claim_number = str(claim_number).strip()
    cached = _CACHE.get("facets_get_duplicate_claim", {"claim_number": claim_number})
    if cached.hit and isinstance(cached.value, dict):
        logger.info("Cache hit", extra={"endpoint": "duplicate_claim"})
        return cached.value

    # Get token
    token, token_status = get_facets_token()
    if not token:
        payload = {
            "error": token_status.get("message") or token_status.get("error") or "Token error",
            "message": token_status.get("message") or token_status.get("error") or "Token error",
            "claim_number": claim_number,
            "endpoint": "duplicate_claim",
            "timestamp": int(time.time()),
            "token_status": token_status,
        }
        return payload

    # Build headers
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

    # Step 1: Get claim summary to extract required parameters
    summary = get_claim_summary(claim_number)
    sbsb_id = _find_first_key(summary, "SBSB_ID")
    grgr_id = _find_first_key(summary, "GRGR_ID")
    clcl_cl_sub_type = _find_first_key(summary, "CLCL_CL_SUB_TYPE")

    if not all([sbsb_id, grgr_id, clcl_cl_sub_type]):
        logger.error("Could not extract required parameters", extra={"endpoint": "duplicate_claim"})
        return {
            "error": "Could not extract required parameters (SBSB_ID, GRGR_ID, CLCL_CL_SUB_TYPE)",
            "claim_number": claim_number,
            "endpoint": "duplicate_claim",
            "extracted": {
                "SBSB_ID": sbsb_id,
                "GRGR_ID": grgr_id,
                "CLCL_CL_SUB_TYPE": clcl_cl_sub_type
            }
        }

    # Step 2: Extract line item date ranges for filtering
    line_date_ranges = _extract_line_date_ranges(summary)

    # Make request to search for duplicate claims
    search_url = f"{FACETS_BASE_URL}/Search/Claims/Inquiry"
    params = {
        "SubscriberID": sbsb_id,
        "GroupID": grgr_id,
        "ClaimType": clcl_cl_sub_type
    }

    # Construct full URL with query parameters
    full_url = f"{search_url}?{urlencode(params)}"

    logger.info("Calling Facets duplicate claim search", extra={"endpoint": "duplicate_claim", "params": params})
    search_resp = _http_get(full_url, headers=headers, timeout=REQUEST_TIMEOUT)

    if search_resp.status_code is None or (search_resp.status_code >= 400):
        meta = _extract_status_meta(search_resp)
        msg = meta.get("status_message")
        payload = {
            "error": msg,
            "message": msg,
            "status_code": meta.get("status_code"),
            "status_message": meta.get("status_message"),
            "claim_number": claim_number,
            "endpoint": "duplicate_claim",
            "timestamp": int(time.time()),
        }
        return payload

    # Step 3: Filter results per line item
    search_body = search_resp.body
    candidate_claims = _extract_duplicate_claim_results(search_body)

    # Build per-line-item results
    line_item_results: List[Dict[str, Any]] = []

    for line_range in line_date_ranges:
        cdml_seq_no = line_range.get("CDML_SEQ_NO")
        cdml_from_dt = _parse_iso_datetime(line_range.get("CDML_FROM_DT"))
        cdml_to_dt = _parse_iso_datetime(line_range.get("CDML_TO_DT"))

        if cdml_from_dt is None or cdml_to_dt is None:
            continue

        # Filter claims for this line item
        line_filtered_claims: List[Dict[str, Any]] = []
        for claim in candidate_claims:
            clcl_low_svc_dt = _parse_iso_datetime(claim.get("CLCL_LOW_SVC_DT"))
            clcl_high_svc_dt = _parse_iso_datetime(claim.get("CLCL_HIGH_SVC_DT"))

            if clcl_low_svc_dt is None or clcl_high_svc_dt is None:
                continue

            # Apply filter: CDML_FROM_DT >= CLCL_LOW_SVC_DT AND CDML_TO_DT <= CLCL_HIGH_SVC_DT
            if cdml_from_dt >= clcl_low_svc_dt and cdml_to_dt <= clcl_high_svc_dt:
                line_filtered_claims.append(claim)

        # Add results for this line item
        line_item_results.append({
            "CDML_SEQ_NO": cdml_seq_no,
            "CDML_FROM_DT": line_range.get("CDML_FROM_DT"),
            "CDML_TO_DT": line_range.get("CDML_TO_DT"),
            "filtered_claims_count": len(line_filtered_claims),
            "filtered_claims": line_filtered_claims
        })

    # Build response
    result = {
        "status_code": search_resp.status_code,
        "claim_number": claim_number,
        "endpoint": "duplicate_claim",
        "timestamp": int(time.time()),
        "search_params": params,
        "total_claims_found_before_filtering": len(candidate_claims),
        "total_line_items": len(line_date_ranges),
        "line_items": line_item_results
    }

    meta = _extract_status_meta(search_resp)
    result["status_message"] = meta.get("status_message")

    logger.info(
        "Facets duplicate claim search response",
        extra={
            "endpoint": "duplicate_claim",
            "status_code": result.get("status_code"),
            "total_found": result.get("total_claims_found"),
            "total_line_items": result.get("total_line_items")
        },
    )

    _CACHE.set("facets_get_duplicate_claim", {"claim_number": claim_number}, result)
    return result


# ==============================================================================
# ==============================================================================
# Create Individual Structured Tools
# ==============================================================================

facets_summary_tool = StructuredTool.from_function(
    func=get_claim_summary,
    name="facets_get_summary",
    description=(
        "Fetch claim summary data(ex. group name,group id,subscriber id,member name,provider name,npi,provider id,TIN etc) from Facets API. "
        "Returns comprehensive claim information including member details, provider, "
        "service dates, diagnosis codes, and line item summaries."
        "Input will be always a single claim number."
    ),
    args_schema=ClaimNumberInput,
)

# facets_providers_tool = StructuredTool.from_function(
#     func=get_claim_providers,
#     name="facets_get_providers",
#     description=(
#         "Fetch claim provider information from Facets API. "
#         "Returns rendering provider, servicing provider, billing provider details, "
#         "including NPI, taxonomy codes, and address information."
#     ),
#     args_schema=ClaimNumberInput,
# )

facets_cob_tool = StructuredTool.from_function(
    func=get_claim_cob,
    name="facets_get_cob",
    description=(
        "Fetch Coordination of Benefits (COB) data(COB amount) from Facets API. "
        "Returns information about other insurance coverage if applicable. "
        "Note: May return 404 if no COB data exists (this is normal)."
    ),
    args_schema=ClaimNumberInput,
)

facets_line_details_tool = StructuredTool.from_function(
    func=get_claim_line_details,
    name="facets_get_line_details",
    description=(
        "Fetch all line item details(from date,to date ,line item sequence,line item charge etc) from Facets API. "
        "Iterates through line sequences (1, 2, 3...) until 404 Not Found to get information of all the line items sequences."
        "Returns detailed pricing, allowances, deductibles, and date of service information for each line item details."
        "Input will be always a single claim number."
    ),
    args_schema=ClaimNumberInput,
)


facets_member_eligibility_tool = StructuredTool.from_function(
    func=get_member_eligibility,
    name="facets_get_member_eligibility",
    description=(
        "Fetch claim member eligibility information from Facets API. "
        "Returns member eligibility details including plan coverage effective dates, plan coverage termination date, eligibility indicator within the time interval."
        "Input will be always a single claim number."
    ),
    args_schema=ClaimNumberInput,
)

facets_get_provider_details_tool = StructuredTool.from_function(
    func=get_provider_details,
    name="facets_get_provider_details",
    description=(
        "Fetch provider details from Facets API using the stored procedure CMCSP_PRV1_SRCH_PRCP_NAME_REMT. "
        "claim_number_for_reference is required and is used for auto-resolution from claim summary and DOC360 parse output. "
        "Allowed values for provider_entity_type: P or G or I or F where P=Practitioner, G=Provider Group, I=IPA, F=Facility. "
        "tax_id should come from DOC360 parse output field `25 FEDERAL TAX ID#`. "
        "This tool auto-fetches DOC360 parse output and extracts only field `25 FEDERAL TAX ID#`. "
        "Provider rows are filtered to keep only records where PRPR_NPI matches ANY of the DOC360 NPI fields: "
        "(1) `24 CONTINUED RENDERING NPI`, (2) `33 NPI`, (3) `11 NPI`, or (4) line-item rendering_npi/svc_npi fields as fallback. "
        "Return information related to all the provider corresponding to the given provider_entity_type and tax_id for that particular claim number."
    ),
    args_schema=ProviderDetailsInput,
)

facets_duplicate_claim_tool = StructuredTool.from_function(
    func=get_duplicate_claim,
    name="facets_get_duplicate_claim",
    description=(
        "Search for potential duplicate claims from Facets API. "
        "Uses claim summary data to search by SubscriberID, GroupID, and ClaimType, "
        "then filters results based on line item service date ranges (CDML_FROM_DT >= CLCL_LOW_SVC_DT and CDML_TO_DT <= CLCL_HIGH_SVC_DT)."
    ),
    args_schema=ClaimNumberInput,
)
