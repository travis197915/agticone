from typing import List, Optional
from pydantic import BaseModel, Field


class ExternalAccountId(BaseModel):
    """External account identifier details."""
    facetsAltId: str = Field(default="", description="Facets alternative ID", alias="facetsAltId")
    riosAcctId: str = Field(default="", description="RIOS account ID", alias="riosAcctId")
    unetPolicyNbr: str = Field(default="", description="UNET policy number", alias="unetPolicyNbr")
    cosmosGrpId: str = Field(default="", description="Cosmos group ID", alias="cosmosGrpId")

    model_config = {
        "populate_by_name": True
    }


class LinxClaimSearchInput(BaseModel):
    """Input schema for LINX claim search by subscriber ID (minimal input allowed)."""
    subscriber_id: str = Field(..., description="Subscriber ID to search for", alias="subscriberId")
    first_name: Optional[str] = Field(None, description="First name of the subscriber (optional)", alias="firstName")
    last_name: Optional[str] = Field(None, description="Last name of the subscriber (optional)", alias="lastName")
    dob: Optional[str] = Field(None, description="Date of birth in MM/DD/YYYY format (optional)")
    start_date: Optional[str] = Field(None, description="Claim search start date in MM/DD/YYYY format (optional)", alias="startDate")
    end_date: Optional[str] = Field(None, description="Claim search end date in MM/DD/YYYY format (optional)", alias="endDate")
    unet_policy_nbr: Optional[str] = Field(default=None, description="UNET policy number (optional)", alias="unetPolicyNbr")
    claim_max_limit: Optional[int] = Field(default=0, description="Maximum number of claims to retrieve (0 = no limit)", alias="claimMaxLimit")
    external_account_id_list: Optional[List[ExternalAccountId]] = Field(
        None,
        description="List of external account IDs (facetsAltId, riosAcctId, unetPolicyNbr, cosmosGrpId)",
        alias="externalAccountIdList"
    )

    model_config = {
        "populate_by_name": True
    }


class LinxClaimSearchOutput(BaseModel):
    """Output schema for LINX claim search results."""
    success: bool = Field(..., description="Whether the API call was successful")
    data: Optional[dict] = Field(None, description="Claim data returned from LINX API")
    error: Optional[str] = Field(None, description="Error message if the call failed")
    cache_hit: bool = Field(default=False, description="Whether the result was served from cache")
