"""Schemas for the CMS Medicare opt-out checker tool."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ProviderOptOutInput(BaseModel):
    """Input schema for the medicare opt-out checker tool."""

    npi: Optional[str] = Field(
        default=None,
        description="10-digit National Provider Identifier (NPI) number.",
    )
    first_name: Optional[str] = Field(
        default=None,
        description="Provider's first name (required if not using NPI).",
    )
    last_name: Optional[str] = Field(
        default=None,
        description="Provider's last name (required if not using NPI).",
    )
    state: Optional[str] = Field(
        default=None,
        description="2-letter state code for filtering results (e.g., CA, NY).",
    )
    specialty: Optional[str] = Field(
        default=None,
        description="Provider specialty for filtering results (e.g., Cardiology).",
    )

    @field_validator("npi")
    @classmethod
    def _validate_npi(cls, v: Optional[str]) -> Optional[str]:
        if v:
            v = str(v).strip()
            if not v:
                return None
            if not v.isdigit():
                raise ValueError("NPI must contain only digits")
            if len(v) != 10:
                raise ValueError("NPI must be exactly 10 digits")
        return v

    @field_validator("state")
    @classmethod
    def _validate_state(cls, v: Optional[str]) -> Optional[str]:
        if v:
            v = v.strip().upper()
            if not v:
                return None
            if not v.isalpha() or len(v) != 2:
                raise ValueError("State code must be exactly 2 letters")
        return v

    @field_validator("first_name", "last_name", "specialty")
    @classmethod
    def _normalize_text(cls, v: Optional[str]) -> Optional[str]:
        if v:
            v = v.strip()
            if not v:
                return None
            if not all(c.isalpha() or c.isspace() or c in "'-." for c in v):
                raise ValueError(
                    "Name can only contain letters, spaces, hyphens, "
                    "apostrophes, and periods"
                )
            return v.upper()
        return v

    @model_validator(mode="after")
    def _check_required_fields(self):
        if not self.npi and not self.last_name:
            raise ValueError(
                "Either NPI or last name must be provided to search for providers"
            )
        return self
