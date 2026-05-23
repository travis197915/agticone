"""Schemas for the CBD (Covered Benefit Document) tool."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class CBDCoverageInput(BaseModel):
    """Input for the Medicare/Commercial coverage check."""

    cpt_codes: List[str] = Field(
        ...,
        description="List of CPT codes to check (non-empty, e.g. ['99213','G0548']).",
        min_length=1,
    )
    group_name: str = Field(
        default="Standard Medicare",
        description="Customer or group name. Defaults to 'Standard Medicare'.",
    )
    plan_name: str = Field(
        default="Standard Medicare",
        description="Medicare plan name. Defaults to 'Standard Medicare'.",
    )
    claim_id: Optional[str] = Field(
        default=None,
        description=(
            "Claim identifier used to determine Line of Business (LOB). "
            "When provided, the tool resolves product / group / plan candidates."
        ),
    )

    @field_validator("cpt_codes")
    @classmethod
    def _validate_cpt_codes(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("At least one CPT code is required")
        out: list[str] = []
        for code in v:
            code = (code or "").strip()
            if not code:
                raise ValueError("CPT code cannot be empty")
            out.append(code.upper())
        return out
