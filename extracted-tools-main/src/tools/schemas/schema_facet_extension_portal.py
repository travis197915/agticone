"""
Schema definitions for Facet Extension Portal Tools
"""

from pydantic import BaseModel, Field


class ProviderInput(BaseModel):
    """Input schema for the Facet Extension Portal Provider tool."""
    provider_id: str = Field(
        description="The Provider ID (PRPR ID) from facet tool, e.g., FAC000022500"
    )


class ProgrammeInput(BaseModel):
    """Input schema for the Facet Extension Portal Programme tool."""
    program_detailed_id: str = Field(
        description="The Program Details ID to query, e.g., 276728"
    )


class ClaimIDInput(BaseModel):
    """Input schema for claim inquiry tools."""
    claim_number: str = Field(
        ...,
        description="Claim number to query (e.g., 25XG44660400)"
    )


class GroupModelInput(BaseModel):
    """Input schema for the Group Model tool."""
    claim_number: str = Field(
        description="The Claim number to query (e.g., 25XG44660400) - used to extract PRPR ID"
    )
