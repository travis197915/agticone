"""Schemas for the cross-prevalence billing tool."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class CrossPrevalenceBillingInput(BaseModel):
    """Input for the CPT-pair cross-billing prevailing-code lookup."""

    sql_dsn: str = Field(
        default="",
        description=(
            "SQL DSN string (server:port;database;user;password). "
            "Ignored when AGENT_TOOLS_SQL_BACKEND=memory."
        ),
    )
    cpt_code_a: str = Field(
        ...,
        description=(
            "First CPT code. If > 5 digits, the last 2 digits are treated "
            "as a required modifier (e.g. '0010459' → CPT '00104' + mod 59)."
        ),
    )
    cpt_code_b: str = Field(
        ...,
        description="Second CPT code, same modifier semantics as cpt_code_a.",
    )
    excel_path: Optional[str] = Field(
        default=None,
        description="Optional bootstrap Excel path (ignored in memory mode).",
    )
