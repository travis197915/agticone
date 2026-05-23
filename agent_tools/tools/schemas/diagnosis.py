"""Schemas for the diagnosis coverage tool."""
from __future__ import annotations

from pydantic import BaseModel, Field


class DiagnosisInput(BaseModel):
    """Input schema for diagnosis coverage queries (ICD-10)."""

    diagnosis_code: str = Field(
        ...,
        description="Diagnosis code to check (e.g., ICD-10 like 'E11.9', 'I10').",
    )
