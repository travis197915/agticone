"""Schemas for the DOC360 claim read tool."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ClaimReadByFlnInput(BaseModel):
    """Input schema for doc360_read_claim_by_fln_dcc."""

    fln_dcc: str = Field(
        ...,
        description="FLN/DCC identifier (10–16 digit numeric).",
        min_length=4,
    )
