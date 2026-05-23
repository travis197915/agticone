"""Schemas for the LLM claim parser and the regex flat-template parser."""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class LlmClaimParseInput(BaseModel):
    """Input schema for the LLM-first HCFA parser."""

    claim_data: Dict[str, Any] = Field(
        ...,
        description=(
            "DOC360 envelope or claim content dict. Must contain either a "
            "'content' key (text) or nested data the tool can normalize."
        ),
    )
    template_name: str = Field(
        default="emc_medical",
        description="Ontology template name (default 'emc_medical').",
    )
    ontology: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional ontology dict. Mock mode ignores this.",
    )
    ontology_path: Optional[str] = Field(
        default=None,
        description="Optional path to ontology YAML file (mock mode ignores this).",
    )


class ClaimParseFlatTemplateInput(BaseModel):
    """Input schema for the deterministic regex parser."""

    claim_data: Dict[str, Any] = Field(
        ...,
        description="DOC360 envelope or claim content dict.",
    )
    template_name: str = Field(
        default="emc_medical",
        description="Template name (default 'emc_medical').",
    )
