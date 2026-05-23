"""Schemas for DOC360 claim tool responses.

These schemas define the structure of responses returned by DOC360 claim tools.
They provide runtime validation and type safety for tool consumers.

For LLM agents: The tool docstrings (not these schemas) are what agents see.
These schemas ensure consistent response structures that agents can rely on.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

try:  # Pydantic v2
    from pydantic import ConfigDict  # type: ignore
except Exception:  # pragma: no cover
    ConfigDict = None  # type: ignore


class TokenResponse(BaseModel):
    """OAuth2 token response from DOC360 token endpoint.

    Used internally by Doc360Client for authentication.
    """

    access_token: str
    token_type: Optional[str] = None
    expires_in: Optional[int] = None
    scope: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


class Metadata(BaseModel):
    """Response metadata for DOC360 API calls.

    Captures timing, content type, and HTTP headers from upstream DOC360 responses.
    """

    responseTimeMs: Optional[int] = None
    contentType: Optional[str] = None
    httpHeaders: Dict[str, Any] = Field(default_factory=dict)


class ErrorDetail(BaseModel):
    """Error detail structure for failed DOC360 responses.

    Used when status='error' to provide structured error information.
    """

    code: Optional[str] = None
    message: str
    triedTypeNames: Optional[List[str]] = None


class ClaimContentEnvelope(BaseModel):
    """Standard envelope for DOC360 claim content responses.

    Returned by doc360_read_claim_by_fln_dcc and submit_electronic_claim.

    Success responses (status='success'):
    - content: parsed claim data (dict/list/str)
    - metadata: timing and headers
    - doc360TypeName: which DOC360 classifier matched

    Error responses (status='error'):
    - content: raw error response from upstream
    - error: structured error detail
    """

    lookupId: Optional[str] = Field(default=None, alias="requestId")
    status: str
    httpStatus: int
    content: Any
    metadata: Optional[Metadata] = None
    doc360TypeName: Optional[str] = Field(
        default=None,
        description="DOC360 document classifier that matched (e.g., 'u_keyed_claim', 'u_edi_claim')"
    )
    error: Optional[ErrorDetail] = None

    if ConfigDict is not None:  # Pydantic v2
        model_config = ConfigDict(populate_by_name=True)
    else:  # Pydantic v1
        class Config:
            allow_population_by_field_name = True


class ClaimReadOutput(BaseModel):
    """Output schema for DOC360 claim read operations.

    Used by read_claim_by_fln_dcc tool and claim_micro_image_id_to_fln_dcc_doc360_parse.

    This is a simplified view of ClaimContentEnvelope focused on the fields
    most relevant for downstream consumers (agents, parsers).
    """

    lookupId: Optional[str] = None
    status: Optional[str] = None
    httpStatus: Optional[int] = None
    doc360TypeName: Optional[str] = None
    content: Optional[Any] = None
    metadata: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None

