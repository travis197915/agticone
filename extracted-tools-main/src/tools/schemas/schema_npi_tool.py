"""
Data models and schemas for NPI Registry.
Includes API response models and LangChain tool schemas.
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class NPIRegistryInput(BaseModel):
    """Input schema for NPI Registry lookup tool."""

    npi: Optional[str] = Field(
        None,
        description="10-digit NPI number to look up. Use this for exact NPI queries."
    )

    first_name: Optional[str] = Field(
        None,
        description="Provider first name (for individual providers). Works best with last name."
    )

    last_name: Optional[str] = Field(
        None,
        description="Provider last name (for individual providers). Can be used alone or with first name."
    )

    organization_name: Optional[str] = Field(
        None,
        description="Organization name (for organizational providers). Use for hospitals, clinics, etc."
    )

    city: Optional[str] = Field(
        None,
        description="City to filter results. Helps narrow down search results."
    )

    state: Optional[str] = Field(
        None,
        description="State abbreviation to filter results (e.g., 'CA', 'NY', 'MA'). 2-letter code."
    )

    limit: int = Field(
        10,
        description="Maximum number of results to return (1-200). Default is 10.",
        ge=1,
        le=200
    )


class NPIRegistryOutput(BaseModel):
    """Output schema for NPI Registry responses."""

    success: bool = Field(description="Whether the query was successful")
    error: Optional[str] = Field(None, description="Error message if query failed")
    count: Optional[int] = Field(None, description="Number of results found (for search queries)")
    provider: Optional[dict] = Field(None, description="Provider information (for single NPI lookup)")
    results: Optional[list] = Field(None, description="List of providers (for search queries)")


class Address(BaseModel):
    """Address model for provider locations."""
    address_line_1: str = ""
    address_line_2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country_code: str = ""
    country_name: str = ""
    telephone_number: str = ""
    fax_number: str = ""
    address_type: str = ""
    address_purpose: str = ""


class Taxonomy(BaseModel):
    """Taxonomy (specialty) model."""
    code: str = ""
    taxonomy_group: str = ""
    description: str = ""
    state: Optional[str] = None
    license: Optional[str] = None
    primary: bool = False


class Identifier(BaseModel):
    """Other identifier model for additional provider identifiers."""
    identifier: str = ""
    type: str = ""
    state: Optional[str] = None
    issuer: str = ""


class Provider(BaseModel):
    """Provider information model."""
    npi: str
    enumeration_type: str
    status: str
    enumeration_date: str = ""
    last_updated: str = ""
    deactivation_date: str = ""
    reactivation_date: str = ""

    first_name: Optional[str] = None
    last_name: Optional[str] = None
    middle_name: Optional[str] = None
    credential: Optional[str] = None
    sole_proprietor: Optional[str] = None
    gender: Optional[str] = None

    organization_name: Optional[str] = None
    organizational_subpart: Optional[str] = None
    authorized_official_first_name: Optional[str] = None
    authorized_official_last_name: Optional[str] = None
    authorized_official_title: Optional[str] = None
    authorized_official_telephone: Optional[str] = None


class NPIResponse(BaseModel):
    """Complete NPI API response model."""
    provider: Provider
    practice_address: Optional[Address] = None
    mailing_address: Optional[Address] = None
    primary_taxonomy: Optional[Taxonomy] = None
    other_taxonomies: List[Taxonomy] = []
    identifiers: List[Identifier] = []
    endpoints: List[Dict[str, Any]] = []
    other_names: List[Dict[str, Any]] = []


class NPIQueryInput(BaseModel):
    """Legacy input schema for backward compatibility."""

    npi: Optional[str] = Field(
        None,
        description="10-digit NPI number to look up. Use this for exact NPI queries."
    )
    first_name: Optional[str] = Field(
        None,
        description="Provider first name (for individual providers)"
    )
    last_name: Optional[str] = Field(
        None,
        description="Provider last name (for individual providers)"
    )
    organization_name: Optional[str] = Field(
        None,
        description="Organization name (for organizational providers)"
    )
    city: Optional[str] = Field(
        None,
        description="City to filter results"
    )
    state: Optional[str] = Field(
        None,
        description="State abbreviation to filter results (e.g., 'CA', 'NY')"
    )
    limit: int = Field(
        10,
        description="Maximum number of results to return (1-200). Default is 10.",
        ge=1,
        le=200
    )


class SimplifiedResponse(BaseModel):
    """Simplified response format."""
    npi_number: str
    provider_first_name: str = ""
    provider_last_name: str = ""
    organization: Optional[str] = None
    status: str
    practice_address: Dict[str, Any] = {}
    mailing_address: Dict[str, Any] = {}
    primary_taxonomy: Dict[str, Any] = {}
    other_taxonomies: List[Dict[str, Any]] = []
