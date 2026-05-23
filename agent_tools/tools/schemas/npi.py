"""Schemas for the NPI registry tool (kept callable but not in the registry)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class NPIRegistryInput(BaseModel):
    """Input schema for NPI Registry lookup tool."""

    npi: Optional[str] = Field(
        None,
        description="10-digit NPI number to look up. Use for exact NPI queries.",
    )
    first_name: Optional[str] = Field(
        None,
        description="Provider first name (for individual providers).",
    )
    last_name: Optional[str] = Field(
        None,
        description="Provider last name (for individual providers).",
    )
    organization_name: Optional[str] = Field(
        None,
        description="Organization name (for organizational providers).",
    )
    city: Optional[str] = Field(None, description="City to filter results.")
    state: Optional[str] = Field(
        None, description="State abbreviation to filter results (e.g., 'CA').",
    )
    limit: int = Field(
        10,
        description="Maximum number of results to return (1–200). Default 10.",
        ge=1, le=200,
    )
