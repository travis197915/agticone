"""Schemas for the LINX BH claim search tool."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ExternalAccountId(BaseModel):
    """External account identifier details (Facets, RIOS, UNET, Cosmos)."""

    facetsAltId: str = Field(default="", description="Facets alternative ID")
    riosAcctId: str = Field(default="", description="RIOS account ID")
    unetPolicyNbr: str = Field(default="", description="UNET policy number")
    cosmosGrpId: str = Field(default="", description="Cosmos group ID")

    model_config = {"populate_by_name": True}


class LinxClaimSearchInput(BaseModel):
    """Input schema for LINX claim search by subscriber ID."""

    subscriber_id: str = Field(
        ..., description="Subscriber ID to search for", alias="subscriberId",
    )
    first_name: Optional[str] = Field(
        None, description="Subscriber first name (optional)", alias="firstName",
    )
    last_name: Optional[str] = Field(
        None, description="Subscriber last name (optional)", alias="lastName",
    )
    dob: Optional[str] = Field(
        None, description="DOB in MM/DD/YYYY format (optional)",
    )
    start_date: Optional[str] = Field(
        None,
        description="Claim search start date in MM/DD/YYYY format (optional)",
        alias="startDate",
    )
    end_date: Optional[str] = Field(
        None,
        description="Claim search end date in MM/DD/YYYY format (optional)",
        alias="endDate",
    )
    unet_policy_nbr: Optional[str] = Field(
        default=None, description="UNET policy number (optional)",
        alias="unetPolicyNbr",
    )
    claim_max_limit: Optional[int] = Field(
        default=0,
        description="Maximum number of claims to retrieve (0 = no limit)",
        alias="claimMaxLimit",
    )
    external_account_id_list: Optional[List[ExternalAccountId]] = Field(
        None,
        description=(
            "List of external account IDs "
            "(facetsAltId, riosAcctId, unetPolicyNbr, cosmosGrpId)."
        ),
        alias="externalAccountIdList",
    )

    model_config = {"populate_by_name": True}
