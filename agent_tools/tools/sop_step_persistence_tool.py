"""
SOP step persistence tool — slim repo-local port (``save_sop_step``).

Idempotent upsert keyed on
``(execution_id, claim_id, agent_name, sop_step_number)``. Runs against
the in-memory SQL shim when ``AGENT_TOOLS_SQL_BACKEND=memory``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import StructuredTool

from . import _sql_memory
from ._logging import get_logger
from .schemas.sop_step import SaveSopStepInput

LOGGER = get_logger("save_sop_step")
TABLE = "sop_step_executions"
KEY_FIELDS = ["execution_id", "claim_id", "agent_name", "sop_step_number"]


def _normalize_dt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat()


def _save(**kwargs: Any) -> dict[str, Any]:
    if not _sql_memory.using_memory():
        return {"ok": False, "skipped": True, "reason": "no_sql_connection"}

    row = {
        **{k: kwargs.get(k) for k in [
            "execution_id", "claim_id", "agent_name",
            "sop_name", "sop_step_number", "sop_step_name", "sop_rule_id",
            "sop_step_description", "sop_action",
            "step_exec_status", "status", "result_summary", "rationale",
            "transaction_time_sec",
        ]},
        "timestamp": _normalize_dt(kwargs.get("timestamp")),
        "started_at": _normalize_dt(kwargs.get("started_at")),
        "ended_at": _normalize_dt(kwargs.get("ended_at")),
        "evidence_refs": json.dumps(kwargs.get("evidence_refs") or []),
        "tools_used": json.dumps(kwargs.get("tools_used") or []),
        "tools_succeeded": json.dumps(kwargs.get("tools_succeeded") or []),
        "tools_failed": json.dumps(kwargs.get("tools_failed") or []),
        "tools_skipped": json.dumps(kwargs.get("tools_skipped") or []),
        "tool_error_details": json.dumps(kwargs.get("tool_error_details") or {}),
    }
    try:
        row_id, action = _sql_memory.upsert(TABLE, KEY_FIELDS, row)
    except Exception as exc:
        return {"ok": False, "skipped": False, "reason": str(exc)}
    return {"ok": True, "action": action, "row_id": row_id}


def build_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="save_sop_step",
        description=(
            "Idempotent upsert of one SOP-step execution row. Keyed on "
            "(execution_id, claim_id, agent_name, sop_step_number). Uses "
            "the in-memory SQL shim in this build."
        ),
        func=lambda **kwargs: _save(**kwargs),
        args_schema=SaveSopStepInput,
    )
