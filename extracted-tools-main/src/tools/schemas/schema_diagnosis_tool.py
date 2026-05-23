"""
Pydantic Data Models for Covered Diagnosis Tool

This module defines the input/output schemas for the diagnosis coverage tool.
"""
from typing import Optional
from pydantic import BaseModel, Field


class DiagnosisInput(BaseModel):
    """
    Structured input for diagnosis coverage query tool.
    """
    diagnosis_code: str = Field(
        ...,
        description="Diagnosis code to check (e.g., ICD-10 code like 'E11.9', 'I10').",
    )


class DiagnosisResult(BaseModel):
    """
    Result for a single diagnosis code lookup.
    """
    diagnosis_code: str = Field(..., description="The diagnosis code queried")
    code_type: Optional[str] = Field(None, description="Type/category of the diagnosis code")
    covered: Optional[str] = Field(None, description="Coverage status")
    description: Optional[str] = Field(None, description="Description of the diagnosis")


class DiagnosisOutput(BaseModel):
    """
    Structured output from diagnosis coverage query.
    """
    success: bool = Field(..., description="Whether the query was successful")
    diagnosis_code: str = Field(..., description="The diagnosis code that was queried")
    result: Optional[DiagnosisResult] = Field(None, description="Diagnosis coverage result if found")
    error: Optional[str] = Field(None, description="Error message if query failed")
