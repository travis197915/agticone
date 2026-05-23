"""Schemas for the Facets family of tools."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import AliasChoices, BaseModel, Field


class ClaimNumberInput(BaseModel):
    """Input schema for claim inquiry tools."""

    claim_number: str = Field(
        ...,
        description="Claim number to query (e.g., 25XG44660400)",
    )


class ProviderDetailsInput(BaseModel):
    """Input schema for provider details tool."""

    provider_entity_type: Optional[Literal["G", "P", "F", "I"]] = Field(
        default=None,
        validation_alias=AliasChoices(
            "provider_entity_type", "PRPR_ENTITY", "prpr_entity",
        ),
        description=(
            "Provider entity type (PRPR_ENTITY). Allowed values: "
            "P=Practitioner, G=Provider Group, I=IPA, F=Facility."
        ),
    )
    tax_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "tax_id", "provider_tin", "tin", "federal_tax_id", "MCTN_ID",
        ),
        description=(
            "Provider tax ID from DOC360 parse output. Aliases: "
            "MCTN_ID / federal_tax_id / tin / provider_tin."
        ),
    )
    claim_number_for_reference: str = Field(
        ...,
        min_length=1,
        description=(
            "Claim number used to auto-resolve provider_entity_type from "
            "Facets summary and tax_id from DOC360 parse output when "
            "direct values are missing."
        ),
    )
