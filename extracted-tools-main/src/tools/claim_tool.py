import os
import time
import json
from typing import Any, Dict, Optional

import httpx
from dotenv import load_dotenv, dotenv_values
import logging
from pydantic import ValidationError

from thynkr_bhagenticai.logging_utils import get_logger
from thynkr_bhagenticai.tool_cache import ToolCache

from .schemas.schema_claim_tool import ClaimContentEnvelope, ClaimReadOutput, ErrorDetail, Metadata


def _find_env_file() -> Optional[str]:
    """Locate a local env file.

    Precedence:
      1) ENV_PATH
      2) repo root `.env.stg`
    """

    explicit = os.getenv("ENV_PATH")
    if explicit:
        return explicit

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(repo_root, ".env.stg")
    if os.path.exists(candidate):
        return candidate

    return None


env_file = _find_env_file()

# Load env file if present, but support running purely from process env vars
_env_values: Dict[str, Optional[str]] = {}
if env_file:
    load_dotenv(env_file)
    _env_values = dotenv_values(env_file)


def _get_config_value(key: str) -> Optional[str]:
    """Return configuration value, preferring process env over `.env.stg`.

    This keeps local `.env.stg` convenient while allowing stg/prod switching via
    runtime environment variables (containers/CI/secret stores).
    """
    return os.getenv(key) or _env_values.get(key)


logger = get_logger(__name__)
if os.getenv("DOC360_DEBUG") == "1":
    logger.setLevel(logging.DEBUG)

DOC360_DOCUMENT_CLASSIFIERS = (
    "u_keyed_claim",
    "u_edi_claim",
    "u_clm_corsp_lwso_doc",
)

_CACHE = ToolCache()


def _build_fln_dcc_filter_criteria(fln_dcc: str) -> Dict[str, Any]:
    """Build DOC360 criteria payload for an FLN/DCC lookup.

    DOC360 expects the FLN/DCC passed via criteria.filterClauses.
    """
    rid = str(fln_dcc).strip()
    return {
        "filterClauses": [
            {
                "type": "equal",
                "name": "u_fln_dcc",
                "value": rid,
            }
        ]
    }


def _envelope_has_content(envelope: Dict[str, Any]) -> bool:
    """Return True when a DOC360 response envelope appears to contain content."""
    if not isinstance(envelope, dict):
        return False
    if envelope.get("status") != "success":
        return False

    content = envelope.get("content")
    if content is None:
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, (list, dict)):
        return len(content) > 0
    return bool(content)


class Doc360Client:
    """Minimal client for DOC360 content endpoints with token caching.

    Usage:
        client = Doc360Client()
        resp = client.submit_electronic_claim({"typeName": "u_keyed_claim", "criteria": {...}})
    """

    def __init__(
        self,
        token_url: Optional[str] = None,
        api_base: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scope: Optional[str] = None,
        read_document_content: Optional[str] = None,
        app_id: Optional[str] = None,
        user_id: Optional[str] = None,
        upstream_env: Optional[str] = None,
        timeout: float = 10.0,
    ):
        """Create a DOC360 client configured via the repo `.env.stg`.

        Args:
            token_url: OAuth2 token endpoint override.
            api_base: DOC360 API base URL override.
            client_id: Must be `None` (credentials must come from `.env.stg` or env vars).
            client_secret: Must be `None` (credentials must come from `.env.stg` or env vars).
            scope: OAuth scope override.
            read_document_content: Path fragment for the DOC360 content read endpoint.
            app_id: DOC360 application id header value override.
            user_id: DOC360 user id header value override.
            upstream_env: Upstream environment header value (e.g., `int`).
            timeout: HTTP request timeout in seconds.
        """

        # Non-secret settings can come from args, process env, or ENV_PATH (`.env.stg` by default).
        self.token_url = token_url or _get_config_value("DOC360_TOKEN_URL")
        self.api_base = api_base or _get_config_value("DOC360_API_BASE")
        self.scope = scope or _get_config_value("DOC360_SCOPE")
        self.read_document_content = (
            read_document_content
            or _get_config_value("DOC360_READ_DOCUMENT_CONTENT")
            or "/api/ecs/doc360-getcontent/v1/document-contents/read"
        )

        self.app_id = app_id or _get_config_value("DOC360_APP_ID")
        self.user_id = user_id or _get_config_value("DOC360_USER_ID")
        self.upstream_env = upstream_env or _get_config_value("UPSTREAM_ENV")

        # Credentials should come from env vars or ENV_PATH (`.env.stg` by default).
        # Fail fast if caller tries to pass them in, to avoid silently ignoring args.
        if client_id is not None or client_secret is not None:
            raise ValueError(
                "Do not pass client_id/client_secret to Doc360Client(). Set them via environment variables or the env file referenced by ENV_PATH instead."
            )

        self.client_id = _get_config_value("DOC360_CLIENT_ID")
        self.client_secret = _get_config_value("DOC360_CLIENT_SECRET")
        self.timeout = timeout

        # token cache
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0

        # httpx client
        self._client = httpx.Client(timeout=self.timeout)

    def _get_token(self) -> str:
        """Return a valid bearer token, refreshing if needed."""
        now = time.time()
        # refresh if token missing or expires in next 60s
        if self._token and now < self._token_expiry - 60:
            return self._token

        if not self.token_url:
            raise RuntimeError("Missing DOC360_TOKEN_URL (set env var or .env.stg)")
        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                "Missing DOC360 credentials (DOC360_CLIENT_ID/DOC360_CLIENT_SECRET). Set env vars or provide an env file via ENV_PATH."
            )
        if not self.upstream_env:
            raise RuntimeError("Missing UPSTREAM_ENV (set env var or .env.stg)")

        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.scope,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Upstream-Env": self.upstream_env,
        }

        resp = self._client.post(self.token_url, data=data, headers=headers)
        # handle non-2xx without raising httpx exception so we can log body
        if resp.status_code < 200 or resp.status_code >= 300:
            logger.error("Token endpoint returned %s: %s", resp.status_code, resp.text)
            raise RuntimeError(f"Failed to obtain token: {resp.status_code} {resp.text}")
        j = resp.json()
        token = j.get("access_token")
        expires_in = j.get("expires_in", 300)
        if not token:
            raise RuntimeError("Failed to obtain access token from DOC360: %s" % (j,))
        self._token = token
        self._token_expiry = now + int(expires_in)
        return self._token

    def _headers(self) -> Dict[str, str]:
        """Build DOC360 request headers (includes bearer token)."""
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Doc360-Client-Application-ID": self.app_id or "",
            "Doc360-Client-User-ID": self.user_id or "",
            "X-Upstream-Env": self.upstream_env,
            "Content-Type": "application/json",
        }

    def submit_electronic_claim(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a content read request to DOC360 and return a normalized envelope.

        body: the JSON payload as described by DOC360 (e.g., typeName + criteria)
        """
        # Keep this tolerant for unit tests (which often stub the http client)
        # while still behaving correctly in real runs.
        base = (self.api_base or "").rstrip("/")
        path = (self.read_document_content or "").strip()
        if path and not path.startswith("/"):
            path = "/" + path
        url = f"{base}{path}" if base else (path or "")

        if not url:
            raise RuntimeError(
                "Missing DOC360_API_BASE and DOC360_READ_DOCUMENT_CONTENT (set env vars or .env.stg)"
            )

        def _preferred_envelope_id(criteria: Any) -> str:
            """Pick a stable identifier for the response envelope.

            Prefer:
              1) FLN/DCC (criteria.u_fln_dcc or in criteria.filterClauses)
              2) generated timestamp (ms)
            """
            if isinstance(criteria, dict):
                fln = criteria.get("u_fln_dcc")
                if fln:
                    return str(fln)

                clauses = criteria.get("filterClauses")
                if isinstance(clauses, list):
                    for clause in clauses:
                        if not isinstance(clause, dict):
                            continue
                        if clause.get("name") == "u_fln_dcc":
                            val = clause.get("value")
                            if val:
                                return str(val)

            return str(int(time.time() * 1000))

        # simple retry loop
        last_err = None
        for attempt in range(3):
            try:
                start = time.time()
                resp = self._client.post(url, json=body, headers=self._headers())
                # If unauthorized, clear token and retry once
                if resp.status_code == 401:
                    self._token = None
                    self._token_expiry = 0
                    if attempt == 0:
                        continue
                # no raise here, handle response content
                if resp.status_code >= 200 and resp.status_code < 300:
                    # Return content as JSON when possible, otherwise return raw text.
                    # This does not fabricate any content; it only parses the upstream body.
                    try:
                        data = resp.json()
                    except Exception:
                        data = resp.text

                    # collect metadata
                    try:
                        headers = dict(resp.headers)
                    except Exception:
                        headers = {}
                    content_type = headers.get("content-type", "")
                    response_time_ms = int((time.time() - start) * 1000)

                    criteria = body.get("criteria", {}) if isinstance(body, dict) else {}
                    envelope = ClaimContentEnvelope(
                        lookupId=_preferred_envelope_id(criteria),
                        status="success",
                        httpStatus=resp.status_code,
                        content=data,
                        metadata=Metadata(
                            responseTimeMs=response_time_ms,
                            contentType=content_type,
                            httpHeaders=headers,
                        ),
                    )
                    try:
                        if hasattr(envelope, "model_dump"):
                            return envelope.model_dump()
                        return envelope.dict()
                    except ValidationError as e:
                        logger.error("Validation error building ClaimContentEnvelope: %s", e)
                        # fall back to raw dict
                        return {
                            "lookupId": envelope.lookupId,
                            "status": envelope.status,
                            "httpStatus": envelope.httpStatus,
                            "content": envelope.content,
                            "metadata": (
                                envelope.metadata.model_dump()
                                if envelope.metadata and hasattr(envelope.metadata, "model_dump")
                                else (envelope.metadata.dict() if envelope.metadata else None)
                            ),
                        }
                else:
                    # return standardized error envelope and log for debugging
                    # Preserve raw upstream body in `content`.
                    try:
                        err = resp.json()
                    except Exception:
                        err = {"message": resp.text}
                    logger.debug("DOC360 non-2xx response: %s %s", resp.status_code, resp.text)
                    return {
                        "lookupId": None,
                        "status": "error",
                        "httpStatus": resp.status_code,
                        "content": resp.text,
                        "error": err,
                    }
            except Exception as e:
                last_err = e
                logger.exception("Exception while calling DOC360: %s", e)
                time.sleep(0.5 * (attempt + 1))
                continue

        raise RuntimeError("submit_electronic_claim failed") from last_err


# Small convenience wrapper for LangGraph integrations
class ClaimTool:
    def __init__(self, client: Optional[Doc360Client] = None):
        """Create a small façade around `Doc360Client` for agent/tool usage.

        Args:
            client: Optional pre-configured `Doc360Client` (useful for tests).
        """
        self.client = client or Doc360Client()

    def read_by_fln_dcc(self, fln_dcc: str) -> Dict[str, Any]:
        """Read claim content by FLN/DCC.

        DOC360 has multiple document classifiers for content read. This method tries
        them in order and returns the first successful response.
        """
        criteria = _build_fln_dcc_filter_criteria(fln_dcc)

        last_response: Optional[Dict[str, Any]] = None
        for type_name in DOC360_DOCUMENT_CLASSIFIERS:
            envelope = self.client.submit_electronic_claim({"typeName": type_name, "criteria": criteria})
            # Include which classifier produced this envelope (useful for debugging).
            # This does not modify the upstream response body.
            if isinstance(envelope, dict):
                envelope["doc360TypeName"] = type_name
            last_response = envelope
            # DOC360 can return 2xx with no/empty content for classifiers that don't match.
            if _envelope_has_content(envelope):
                return envelope

        if last_response is None:
            last_response = {
                "lookupId": str(fln_dcc).strip(),
                "status": "error",
                "httpStatus": 500,
                "error": {"message": "No DOC360 classifiers attempted"},
            }

        # Preserve existing envelope but include what was tried.
        err = last_response.get("error")
        if not isinstance(err, dict):
            err = {"message": str(err)}
        err["triedTypeNames"] = list(DOC360_DOCUMENT_CLASSIFIERS)
        last_response["error"] = err
        return last_response

    def get_electronic_claim_status(self, lookup_id: str) -> Dict[str, Any]:
        """Return a minimal status for a previously read electronic claim.

        DOC360 content reads are synchronous in this repo, so this is a convenience
        helper for agent flows that expect a status check.

        Args:
            lookup_id: The envelope identifier (usually the FLN/DCC used to look up content).

        Returns:
            A small status dict.
        """
        return {"lookupId": lookup_id, "status": "completed", "details": {}}


from langchain.tools import tool
from pydantic import BaseModel, Field


def _data_dir() -> str:
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, "data")


def _safe_write_json(path: str, payload: Any) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except Exception:
        # Never fail the tool call due to local filesystem issues.
        return


def _safe_write_text(path: str, text: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        # Never fail the tool call due to local filesystem issues.
        return


class ClaimReadByFlnInput(BaseModel):
    fln_dcc: str = Field(..., description="FLN/DCC identifier (10-16 digit numeric)")


def _read_claim_by_fln_adapter(fln_dcc: str) -> ClaimReadOutput:
    """Read claim content using FLN/DCC and return a typed output model.

    Args:
        fln_dcc: The FLN/DCC identifier.

    Returns:
        `ClaimReadOutput` populated from the normalized envelope.
    """
    tool = ClaimTool()
    rid = str(fln_dcc).strip()
    logger.info("Calling DOC360 read_by_fln_dcc")
    envelope = tool.read_by_fln_dcc(rid)
    if isinstance(envelope, dict):
        logger.info(
            "DOC360 read_by_fln_dcc response",
            extra={"status": envelope.get("status"), "httpStatus": envelope.get("httpStatus")},
        )
    return ClaimReadOutput(
        lookupId=envelope.get("lookupId") or envelope.get("requestId"),
        status=envelope.get("status"),
        httpStatus=envelope.get("httpStatus"),
        doc360TypeName=envelope.get("doc360TypeName"),
        content=envelope.get("content"),
        metadata=envelope.get("metadata"),
    )


@tool("doc360_read_claim_by_fln_dcc", args_schema=ClaimReadByFlnInput)
def read_claim_by_fln_dcc(fln_dcc: str) -> Dict[str, Any]:
    """Read a DOC360 claim by FLN/DCC and return a normalized envelope.

    Args:
        fln_dcc: The FLN/DCC identifier.

    Returns:
        A JSON-serializable dict containing `lookupId`, `status`, `httpStatus`, `content`, and `metadata`.
    """
    rid = str(fln_dcc).strip()
    cache_key = {"fln_dcc": rid}
    cached = _CACHE.get("doc360_read_claim_by_fln_dcc", cache_key)
    if cached.hit and isinstance(cached.value, dict):
        logger.info("DOC360 cache hit")
        return cached.value

    out = _read_claim_by_fln_adapter(fln_dcc)
    # ToolNode expects JSON-serializable content.
    # Return a plain dict rather than a Pydantic model instance.
    if hasattr(out, "model_dump"):
        result = out.model_dump()  # type: ignore[assignment]
    elif hasattr(out, "dict"):
        result = out.dict()  # type: ignore[assignment]
    else:
        result = out  # type: ignore[assignment]

    # Persist artifacts for agent/tool runs (mirrors runner behavior).
    rid = rid or (result.get("lookupId") if isinstance(result, dict) else None) or "unknown"
    data_dir = _data_dir()
    _safe_write_json(os.path.join(data_dir, f"doc360_envelope-{rid}.json"), result)

    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, str):
            _safe_write_text(os.path.join(data_dir, f"doc360_content-{rid}.txt"), content)
        else:
            _safe_write_text(
                os.path.join(data_dir, f"doc360_content-{rid}.txt"),
                json.dumps(content, indent=2, default=str),
            )

    # Cache only successful reads.
    if isinstance(result, dict) and result.get("status") == "success":
        _CACHE.set("doc360_read_claim_by_fln_dcc", cache_key, result)

    return result  # type: ignore[return-value]
