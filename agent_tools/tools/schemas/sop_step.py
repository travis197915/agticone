"""Schemas for the save_sop_step persistence tool."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SaveSopStepInput(BaseModel):
    """Input for an idempotent SOP-step execution row upsert."""

    sql_dsn: str = Field(
        default="",
        description=(
            "SQL DSN string (server:port;database;user;password). "
            "Ignored when AGENT_TOOLS_SQL_BACKEND=memory."
        ),
    )
    execution_id: str = Field(..., description="Stable execution id.")
    claim_id: str = Field(..., description="Claim id under audit.")
    agent_name: str = Field(..., description="Agent that executed the step.")

    sop_name: Optional[str] = None
    sop_step_number: Optional[int] = None
    sop_step_name: Optional[str] = None
    sop_rule_id: Optional[str] = None
    sop_step_description: Optional[str] = None
    sop_action: Optional[str] = None
    step_exec_status: Optional[str] = None
    status: Optional[str] = None
    result_summary: Optional[str] = None
    rationale: Optional[str] = None
    evidence_refs: List[str] = Field(default_factory=list)

    timestamp: Optional[str] = Field(
        default=None,
        description="ISO-8601 timestamp the step finished at.",
    )
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    transaction_time_sec: Optional[float] = None

    tools_used: List[str] = Field(default_factory=list)
    tools_succeeded: List[str] = Field(default_factory=list)
    tools_failed: List[str] = Field(default_factory=list)
    tools_skipped: List[str] = Field(default_factory=list)
    tool_error_details: Dict[str, Any] = Field(default_factory=dict)
