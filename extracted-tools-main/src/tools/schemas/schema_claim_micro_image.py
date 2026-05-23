"""Pydantic schemas for the claim_micro_image_id_to_fln_dcc_doc360_parse tool.

Defines the **full output contract** so that downstream agents and the
normalizer can rely on typed, validated responses.

Hierarchy
---------
ClaimMicroImageOutput (top-level tool response)
  ├── ParsedClaimPayload        (``parsed`` key – merged regex + LLM output)
  │   ├── ClaimFieldValue       (each flat field in ``fields``)
  │   ├── DiagnosisEntry        (each item in ``diagnoses``)
  │   ├── LineItemEntry         (each item in ``line_items``)
  │   ├── ClaimTotals           (``totals``)
  │   ├── OtherInsurance        (``other_insurance``)
  │   ├── HCPPricing            (``hcp_pricing``)
  │   └── ConfidenceScores      (``confidence_scores``)
  └── ToolErrorDetail           (``error`` key on failure)
      └── UpstreamDetail        (optional upstream HTTP context)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sub-models for the ``parsed`` payload
# ---------------------------------------------------------------------------


class ClaimFieldValue(BaseModel):
    """A single extracted claim field with confidence and provenance."""

    value: Optional[Union[str, List[str]]] = Field(
        default=None,
        description=(
            "Extracted value.  Usually a string; occasionally a list of strings "
            "for multi-valued fields (e.g. NDC UNITS).  None when not found."
        ),
    )
    confidence: Optional[float] = Field(
        default=None,
        description="Extraction confidence score (0.0–1.0).",
    )
    provenance: Optional[Union[Dict[str, Any], List[str]]] = Field(
        default=None,
        description=(
            "Evidence supporting the extraction.  "
            "Dict with 'evidence' key (LLM parser) or list of strings (regex parser)."
        ),
    )


class DiagnosisEntry(BaseModel):
    """A single ICD diagnosis code extracted from Box 21."""

    pointer: Optional[int] = Field(
        default=None,
        description="1-based diagnosis pointer (matches 24E line-item references).",
    )
    code: Optional[str] = Field(
        default=None,
        description="ICD-10 diagnosis code (e.g. 'F332').",
    )
    provenance: Optional[Union[Dict[str, Any], List[str]]] = Field(
        default=None,
        description="Raw text evidence used for extraction.",
    )
    evidence: Optional[str] = Field(
        default=None,
        description="Verbatim text segment (regex parser only).",
    )


class LineItemEntry(BaseModel):
    """A single service line (Box 24) from the parsed claim.

    Contains dates, procedure, charges, units, and financial details
    for one line item.  These fields are the authoritative source for
    per-line DOC360 data (they do NOT appear in ``parsed.fields``).
    """

    from_date: Optional[str] = Field(
        default=None,
        description="Service start date (MMDDYY, e.g. '030325').",
    )
    to_date: Optional[str] = Field(
        default=None,
        description="Service end date (MMDDYY, e.g. '030325').",
    )
    place_of_service: Optional[str] = Field(
        default=None,
        description="CMS Place of Service code (e.g. '11' = Office).",
    )
    type_of_service: Optional[str] = Field(
        default=None,
        description="Type of service code.",
    )
    cpt_hcpcs: Optional[str] = Field(
        default=None,
        description="CPT or HCPCS procedure code (e.g. '99215').",
    )
    modifiers: List[str] = Field(
        default_factory=list,
        description="Procedure modifier codes.",
    )
    diag_pointer: Optional[str] = Field(
        default=None,
        description="Raw diagnosis pointer string from Box 24E (e.g. '1230').",
    )
    diag_pointers: List[int] = Field(
        default_factory=list,
        description="Parsed diagnosis pointer integers (e.g. [1, 2, 3]).",
    )
    charge_amount: Optional[float] = Field(
        default=None,
        description="Line-item charge amount (Box 24F).",
    )
    units: Optional[float] = Field(
        default=None,
        description="Service units / days (Box 24G).",
    )
    anesthesia_time: Optional[str] = Field(
        default=None,
        description="Anesthesia time in minutes (string, e.g. '0000').",
    )
    emg_ind: Optional[str] = Field(
        default=None,
        description="Emergency indicator ('Y' or 'N').",
    )
    line_item_control_no: Optional[str] = Field(
        default=None,
        description="Line-item control number.",
    )
    other_ins_allowed: Optional[float] = Field(
        default=None,
        description="Amount allowed by other insurance for this line.",
    )
    negotiated_rate_ind: Optional[str] = Field(
        default=None,
        description="Negotiated rate reduction indicator.",
    )
    deductible_amount: Optional[float] = Field(
        default=None,
        description="Per-line deductible amount.",
    )
    paid_amount: Optional[float] = Field(
        default=None,
        description="Per-line paid amount.",
    )
    epsdt_ind: Optional[str] = Field(
        default=None,
        description="EPSDT indicator.",
    )
    family_planning_ind: Optional[str] = Field(
        default=None,
        description="Family planning indicator.",
    )
    remarks: Optional[str] = Field(
        default=None,
        description="Line-item remarks / description.",
    )
    rendering_npi: Optional[str] = Field(
        default=None,
        description="Rendering provider NPI for this line (LLM parser only).",
    )
    svc_npi: Optional[str] = Field(
        default=None,
        description="Service facility NPI for this line (LLM parser only).",
    )
    remark_ref_cd: Optional[str] = Field(
        default=None,
        description="Remark reference code (regex parser only).",
    )
    provenance: Optional[Union[Dict[str, Any], List[str]]] = Field(
        default=None,
        description="Extraction provenance / evidence.",
    )
    evidence: Optional[str] = Field(
        default=None,
        description="Verbatim matched text (regex parser only).",
    )


class OtherInsuranceTotal(BaseModel):
    """Sub-totals for other insurance amounts."""

    paid: Optional[float] = Field(default=None)
    allowed: Optional[float] = Field(default=None)


class ClaimTotals(BaseModel):
    """Claim-level financial totals (Box 28-30)."""

    total_charge: Optional[float] = Field(
        default=None,
        description="Total billed charge (Box 28).",
    )
    total_patient_paid: Optional[float] = Field(
        default=None,
        description="Total amount paid by patient (Box 29).",
    )
    total_other_insurance: Optional[OtherInsuranceTotal] = Field(
        default=None,
        description="Other insurance paid / allowed (Box 30).",
    )


class OtherInsurance(BaseModel):
    """Other insurance / COB information (S-record section)."""

    plan_name: Optional[str] = Field(default=None)
    claim_number: Optional[str] = Field(default=None)
    payer_resp_seq: Optional[str] = Field(default=None)
    route_ind: Optional[str] = Field(default=None)
    total_oi_paid: Optional[float] = Field(default=None)
    total_deductible: Optional[float] = Field(default=None)
    contractual_adj: Optional[float] = Field(default=None)
    interest_paid: Optional[float] = Field(default=None)
    total_coinsurance: Optional[float] = Field(default=None)
    remark_codes: List[str] = Field(default_factory=list)
    adjustment_indicator: Optional[str] = Field(default=None)
    adjustment_orig_payment: Optional[float] = Field(default=None)
    remittance_remark_codes: List[str] = Field(default_factory=list)


class HCPPricing(BaseModel):
    """HCP / repricing information section."""

    icn: Optional[str] = Field(default=None, description="Internal control number.")
    price_method: Optional[str] = Field(default=None)
    repriced_allowed_amt: Optional[float] = Field(default=None)
    savings_amount: Optional[float] = Field(default=None)
    reference_id: Optional[str] = Field(default=None)
    rate: Optional[float] = Field(default=None)
    approved_drg_amt: Optional[float] = Field(default=None)
    reject_code: Optional[str] = Field(default=None)
    policy_compliance_code: Optional[str] = Field(default=None)
    exception_code: Optional[str] = Field(default=None)


class ConfidenceScores(BaseModel):
    """Overall and per-field extraction confidence."""

    overall: Optional[float] = Field(
        default=None,
        description="Weighted average confidence across all fields.",
    )
    field_level: Dict[str, float] = Field(
        default_factory=dict,
        description="Per-field confidence scores keyed by field name.",
    )


class ParsedClaimPayload(BaseModel):
    """The merged (regex + LLM) parsed claim payload.

    This is the ``parsed`` dict inside a successful tool response.
    Contains flat header/form fields, structured line items, diagnoses,
    totals, and confidence metadata.
    """

    template_name: Optional[str] = Field(
        default=None,
        description="Parser template used (e.g. 'emc_medical').",
    )
    claim_source: Optional[str] = Field(
        default=None,
        description="Claim origin (e.g. 'physician', 'doc360_print_image').",
    )
    fields: Dict[str, ClaimFieldValue] = Field(
        default_factory=dict,
        description=(
            "Flat claim fields keyed by DOC360 form name "
            "(e.g. '2 PATIENTS NAME (LFM)', '28 TOT CHARGE'). "
            "Each value contains extracted text, confidence, and provenance."
        ),
    )
    diagnoses: List[DiagnosisEntry] = Field(
        default_factory=list,
        description="ICD diagnosis codes from Box 21 (structured list).",
    )
    line_items: List[LineItemEntry] = Field(
        default_factory=list,
        description=(
            "Service lines from Box 24 (structured list). "
            "Per-line dates, charges, units, paid amounts live here — "
            "NOT in the flat ``fields`` dict."
        ),
    )
    totals: Optional[ClaimTotals] = Field(
        default=None,
        description="Claim-level financial totals (Box 28-30).",
    )
    unmapped_fields: Dict[str, Any] = Field(
        default_factory=dict,
        description="Fields the parser found but could not map to a known form position.",
    )
    confidence_scores: Optional[ConfidenceScores] = Field(
        default=None,
        description="Overall and per-field extraction confidence.",
    )
    other_insurance: Optional[OtherInsurance] = Field(
        default=None,
        description="Other insurance / COB information (LLM parser only).",
    )
    hcp_pricing: Optional[HCPPricing] = Field(
        default=None,
        description="HCP / repricing information (LLM parser only).",
    )


# -----------------------------------------------------------------------------
# Error sub-models
# -----------------------------------------------------------------------------


class UpstreamDetail(BaseModel):
    """Optional metadata from the upstream HTTP call that failed."""

    status_code: Optional[int] = Field(default=None)
    endpoint: Optional[str] = Field(default=None)
    status: Optional[str] = Field(default=None)
    httpStatus: Optional[int] = Field(default=None)
    lookupId: Optional[str] = Field(default=None)


class ToolErrorDetail(BaseModel):
    """Structured error when the tool cannot produce a parsed result."""

    code: str = Field(
        ...,
        description=(
            "Machine-readable error code.  One of: "
            "MISSING_CLAIM_NUMBER, FACETS_CALL_FAILED, FACETS_BAD_RESPONSE, "
            "FACETS_ERROR, MICRO_IMAGE_ID_NOT_FOUND, FLN_DCC_CONVERSION_FAILED, "
            "DOC360_CALL_FAILED, DOC360_READ_FAILED, PARSE_FAILED, PARSE_EMPTY."
        ),
    )
    message: str = Field(
        ...,
        description="Human-readable error description.",
    )


# -----------------------------------------------------------------------------
# Top-level tool response
# -----------------------------------------------------------------------------


class ClaimMicroImageOutput(BaseModel):
    """Output schema for ``claim_micro_image_id_to_fln_dcc_doc360_parse``.

    Success (status='success'):
        All identity fields are populated and ``parsed`` contains the
        full merged claim payload.

    Error (status='error'):
        ``error`` is populated with a machine-readable code and message.
        Identity fields and passthrough payloads are present depending
        on how far the pipeline progressed before failure.
    """

    status: str = Field(
        ...,
        description="'success' or 'error'.",
    )
    timestamp: str = Field(
        ...,
        description="ISO-8601 UTC timestamp of tool execution.",
    )
    timestamp_unix: int = Field(
        ...,
        description="Unix epoch seconds at tool execution.",
    )

    # Identity fields (populated progressively as pipeline advances)
    claim_number: Optional[str] = Field(
        default=None,
        description="Input Facets claim number.",
    )
    micro_image_id: Optional[str] = Field(
        default=None,
        description="Micro Image ID extracted from Facets summary.",
    )
    fln_dcc: Optional[str] = Field(
        default=None,
        description="FLN/DCC derived from Micro Image ID.",
    )

    # Success payload
    parsed: Optional[ParsedClaimPayload] = Field(
        default=None,
        description="Merged regex + LLM parsed claim payload (present on success).",
    )

    # Error payload
    error: Optional[ToolErrorDetail] = Field(
        default=None,
        description="Structured error detail (present on error).",
    )
    upstream: Optional[UpstreamDetail] = Field(
        default=None,
        description="Upstream HTTP metadata when the error originated from an external call.",
    )

    # Passthrough payloads (present on error, depending on failure point)
    facets_response: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Raw Facets response (on Facets-stage errors).",
    )
    doc360_envelope: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Raw DOC360 envelope (on DOC360/parse-stage errors).",
    )
    parser_output: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Raw parser output (on PARSE_EMPTY errors).",
    )
