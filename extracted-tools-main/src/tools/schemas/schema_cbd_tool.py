"""
Pydantic Data Models for CBD Coverage Tool

This module defines the input/output schemas for the LangChain tool.
"""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator


class CBDCoverageInput(BaseModel):
    """
    Structured input for CBD coverage query tool.
    """
    cpt_codes: List[str] = Field(
        ...,
        description="List of CPT (Current Procedural Terminology) codes to check coverage. "
                    "CPT codes are procedure codes (e.g., '99213', '99214', 'G0548').",
        min_length=1
    )
    group_name: str = Field(
        default="Standard Medicare",
        description="Customer or group name. Use 'Standard Medicare' as default if not specified."
    )
    plan_name: str = Field(
        default="Standard Medicare",
        description="Medicare plan name. Use 'Standard Medicare' as default if not specified."
    )
    claim_id: Optional[str] = Field(
        default=None,
        description="Claim identifier used to determine the Line of Business (LOB/product) "
                    "for this claim. When provided, the tool calls determine_lob(claim_id) "
                    "to select the correct group/plan candidates."
    )

    @field_validator('cpt_codes')
    def validate_cpt_codes(cls, v):
        """Validate CPT code format."""
        if not v:
            raise ValueError("At least one CPT code is required")
        validated = []
        for code in v:
            code = code.strip()
            if not code:
                raise ValueError("CPT code cannot be empty")
            validated.append(code.upper())
        return validated


class CPTCoverageResult(BaseModel):
    """
    Coverage result for a single CPT code based on actual API response structure.
    """
    cpt_code: str = Field(..., description="The CPT/procedure code (descCode)")
    covered: str = Field(..., description="Coverage status (Yes/No)")
    authorization: str = Field(..., description="Authorization requirements (Yes/No)")
    desc_name: Optional[str] = Field(None, description="Description of the procedure")
    service_type: Optional[str] = Field(None, description="Type of service")
    asam_level: Optional[str] = Field(None, description="ASAM level")
    diagnosis: Optional[str] = Field(None, description="Diagnosis category")
    effective_date: Optional[str] = Field(None, description="Effective date")
    term_date: Optional[str] = Field(None, description="Termination date")
    lob: Optional[str] = Field(None, description="Line of Business")
    market: Optional[str] = Field(None, description="Market")


class CBDCoverageOutput(BaseModel):
    """
    Structured output from CBD coverage query.
    """
    success: bool = Field(..., description="Whether the query was successful")
    group_name: str = Field(..., description="Customer/Group name used in query")
    plan_name: str = Field(..., description="Plan name used in query")
    total_codes_queried: int = Field(..., description="Total number of CPT codes queried")
    codes_found: int = Field(..., description="Number of CPT codes found in results")
    coverage_details: List[CPTCoverageResult] = Field(
        default_factory=list,
        description="Detailed coverage information for each CPT code found"
    )
    not_found_codes: List[str] = Field(
        default_factory=list,
        description="CPT codes that were not found in the coverage data"
    )
    errors: List[str] = Field(
        default_factory=list,
        description="List of errors or warnings encountered"
    )
