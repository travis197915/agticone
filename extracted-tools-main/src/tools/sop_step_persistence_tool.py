from __future__ import annotations

"""
Persist one SOP step execution row into SQL Server (idempotent within an execution).
Fixes common NVARCHAR(4000) truncation by explicitly CASTING JSON params to NVARCHAR(MAX).

Key:
  (execution_id, claim_id, agent_name, sop_step_number)

This module is intended to be invoked by the Supervisor via a StructuredTool:
  save_sop_step = StructuredTool.from_function(...)

Behavior:
- Creates/updates a single row in sop_step_executions.
- Stores evidence_refs and tool arrays as JSON strings (NVARCHAR(MAX)).
- Normalizes ISO timestamps (with optional 'Z') to naive UTC DATETIME2.
"""

from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional

import pymssql
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool

from thynkr_bhagenticai.logging_utils import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------------------
# Pydantic input schema (v2)
# ---------------------------------------------------------------------------------------
class SaveSopStepInput(BaseModel):
    sql_dsn: str = Field(
        ...,
        description="SQL DSN in format server:port;database;user;password",
    )

    execution_id: str
    claim_id: str
    agent_name: str

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
    evidence_refs: Optional[list[str]] = None
    timestamp: Optional[str] = None

    # Step-level timing
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    transaction_time_sec: Optional[float] = None

    # First-class tool arrays (persisted as JSON)
    tools_used: Optional[list[str]] = None
    tools_succeeded: Optional[list[str]] = None
    tools_failed: Optional[list[str]] = None
    tools_skipped: Optional[list[str]] = None
    tool_error_details: Optional[Dict[str, str]] = None


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _get_sql_connection(sql_dsn: str) -> Optional[pymssql.Connection]:
    """
    Create SQL Server connection from DSN.
    DSN format: server:port;database;user;password
    """
    if not sql_dsn or not str(sql_dsn).strip():
        logger.warning("sop_step_persistence_no_dsn")
        return None

    parts = str(sql_dsn).split(";")
    if len(parts) != 4:
        logger.error(
            "sop_step_persistence_invalid_dsn_format",
            extra={"expected": "server:port;database;user;password"},
        )
        return None

    server_port, database, user, password = parts
    if ":" in server_port:
        server, port_s = server_port.split(":", 1)
        port = int(port_s)
    else:
        server = server_port
        port = 1433

    try:
        conn = pymssql.connect(
            server=server,
            port=port,
            database=database,
            user=user,
            password=password,
            timeout=30,
            login_timeout=30,
            tds_version="7.3",
            conn_properties="",
        )
        return conn
    except Exception as e:
        logger.warning("sop_step_persistence_sql_connection_failed", extra={"error": str(e)})
        return None


def _json_dumps_safe(value: Any) -> str:
    """
    Safe JSON dumps for lists/dicts. Defaults to [] for None.
    Ensures a valid JSON string is returned even on error.
    """
    try:
        if value is None:
            return "[]"
        # Light normalization for lists: coerce to strings to avoid unexpected types
        if isinstance(value, list):
            return json.dumps([str(x) for x in value])
        return json.dumps(value)
    except Exception:
        return "[]"


def _parse_iso_utc_to_naive_dt(value: Optional[str]) -> Optional[datetime]:
    """
    Parse an ISO-8601 timestamp (with optional 'Z') into a naive UTC datetime.
    SQL Server DATETIME2 has no timezone; we store UTC as naive.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Convert trailing 'Z' to +00:00 for fromisoformat
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------
def save_sop_step_func(
    *,
    sql_dsn: str,
    execution_id: str,
    claim_id: str,
    agent_name: str,
    sop_name: Optional[str] = None,
    sop_step_number: Optional[int] = None,
    sop_step_name: Optional[str] = None,
    sop_rule_id: Optional[str] = None,
    sop_step_description: Optional[str] = None,
    sop_action: Optional[str] = None,
    step_exec_status: Optional[str] = None,
    status: Optional[str] = None,
    result_summary: Optional[str] = None,
    rationale: Optional[str] = None,
    evidence_refs: Optional[List[str]] = None,
    timestamp: Optional[str] = None,
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    transaction_time_sec: Optional[float] = None,
    tools_used: Optional[List[str]] = None,
    tools_succeeded: Optional[List[str]] = None,
    tools_failed: Optional[List[str]] = None,
    tools_skipped: Optional[List[str]] = None,
    tool_error_details: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Idempotent upsert into SQL Server table sop_step_executions.

    Idempotency Key:
      (execution_id, claim_id, agent_name, sop_step_number)

    Returns:
      {"ok": True, "action": "inserted|updated", "row_id": <id>}
      or {"ok": False, "skipped": True, "reason": "..."} on skip
      or {"ok": False, "skipped": False, "reason": "..."} on error
    """
    logger.info(
        "sop_step_persistence_saving",
        extra={
            "execution_id": execution_id,
            "agent_name": agent_name,
            "sop_step_number": sop_step_number,
            "sop_step_name": sop_step_name,
        },
    )

    conn = _get_sql_connection(sql_dsn)
    if not conn:
        logger.warning("sop_step_persistence_skipped", extra={"reason": "no_sql_connection"})
        return {"ok": False, "skipped": True, "reason": "no_sql_connection"}

    # Normalize/encode JSON payloads
    ev_json = _json_dumps_safe(evidence_refs or [])
    tools_used_json = _json_dumps_safe(tools_used or [])
    tools_succeeded_json = _json_dumps_safe(tools_succeeded or [])
    tools_failed_json = _json_dumps_safe(tools_failed or [])
    tools_skipped_json = _json_dumps_safe(tools_skipped or [])
    tool_error_details_json = _json_dumps_safe(tool_error_details or {})

    # Helpful size logs for triage (large arrays commonly exceed 4000 chars)
    try:
        logger.debug(
            "sop_step_json_sizes_bytes",
            extra={
                "evidence_refs_len": len(ev_json.encode("utf-8")),
                "tools_used_len": len(tools_used_json.encode("utf-8")),
                "tools_succeeded_len": len(tools_succeeded_json.encode("utf-8")),
                "tools_failed_len": len(tools_failed_json.encode("utf-8")),
                "tools_skipped_len": len(tools_skipped_json.encode("utf-8")),
                "tool_error_details_len": len(tool_error_details_json.encode("utf-8")),
            },
        )
    except Exception:
        pass

    sop_step_number_val = int(sop_step_number) if sop_step_number is not None else None

    # Normalize timestamps to DATETIME2 (naive UTC)
    timestamp_dt = _parse_iso_utc_to_naive_dt(timestamp)
    started_at_dt = _parse_iso_utc_to_naive_dt(started_at)
    ended_at_dt = _parse_iso_utc_to_naive_dt(ended_at)

    cursor: Optional[pymssql.Cursor] = None
    try:
        cursor = conn.cursor()

        # Idempotent check
        check_sql = """
        SELECT TOP 1 id
        FROM sop_step_executions
        WHERE execution_id = %s
          AND claim_id = %s
          AND agent_name = %s
          AND sop_step_number = %s
        ORDER BY id DESC
        """
        cursor.execute(check_sql, (execution_id, claim_id, agent_name, sop_step_number_val))
        existing = cursor.fetchone()

        if existing:
            row_id = int(existing[0])
            update_sql = """
            UPDATE sop_step_executions
            SET sop_name = %s,
                sop_step_name = %s,
                sop_rule_id = %s,
                sop_step_description = %s,
                sop_action = %s,
                step_exec_status = %s,
                status = %s,
                result_summary = %s,
                rationale = %s,
                evidence_refs = CAST(%s AS NVARCHAR(MAX)),
                timestamp = %s,
                started_at = %s,
                ended_at = %s,
                transaction_time_sec = %s,
                tools_used = CAST(%s AS NVARCHAR(MAX)),
                tools_succeeded = CAST(%s AS NVARCHAR(MAX)),
                tools_failed = CAST(%s AS NVARCHAR(MAX)),
                tools_skipped = CAST(%s AS NVARCHAR(MAX)),
                tool_error_details = CAST(%s AS NVARCHAR(MAX))
            WHERE id = %s
            """
            cursor.execute(
                update_sql,
                (
                    sop_name,
                    sop_step_name,
                    sop_rule_id,
                    sop_step_description,
                    sop_action,
                    step_exec_status,
                    status,
                    result_summary,
                    rationale,
                    ev_json,
                    timestamp_dt,
                    started_at_dt,
                    ended_at_dt,
                    transaction_time_sec,
                    tools_used_json,
                    tools_succeeded_json,
                    tools_failed_json,
                    tools_skipped_json,
                    tool_error_details_json,
                    row_id,
                ),
            )
            conn.commit()
            logger.info(
                "sop_step_persistence_updated",
                extra={"id": row_id, "execution_id": execution_id, "step": sop_step_number_val},
            )
            return {"ok": True, "action": "updated", "row_id": row_id}

        insert_sql = """
        INSERT INTO sop_step_executions
        (execution_id, claim_id, agent_name, sop_name,
         sop_step_number, sop_step_name, sop_rule_id,
         sop_step_description, sop_action,
         step_exec_status, status,
         result_summary, rationale, evidence_refs, timestamp,
         started_at, ended_at, transaction_time_sec,
         tools_used, tools_succeeded, tools_failed,
         tools_skipped, tool_error_details)
        VALUES (%s, %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, CAST(%s AS NVARCHAR(MAX)), %s,
            %s, %s, %s,
            CAST(%s AS NVARCHAR(MAX)), CAST(%s AS NVARCHAR(MAX)), CAST(%s AS NVARCHAR(MAX)),
            CAST(%s AS NVARCHAR(MAX)), CAST(%s AS NVARCHAR(MAX)))
        """
        cursor.execute(
            insert_sql,
            (
                execution_id,
                claim_id,
                agent_name,
                sop_name,
                sop_step_number_val,
                sop_step_name,
                sop_rule_id,
                sop_step_description,
                sop_action,
                step_exec_status,
                status,
                result_summary,
                rationale,
                ev_json,
                timestamp_dt,
                started_at_dt,
                ended_at_dt,
                transaction_time_sec,
                tools_used_json,
                tools_succeeded_json,
                tools_failed_json,
                tools_skipped_json,
                tool_error_details_json,
            ),
        )
        conn.commit()

        # Retrieve identity
        cursor.execute("SELECT SCOPE_IDENTITY()")
        new_id_row = cursor.fetchone()
        row_id = int(new_id_row[0]) if new_id_row and new_id_row[0] is not None else None

        logger.info(
            "sop_step_persistence_inserted",
            extra={"id": row_id, "execution_id": execution_id, "step": sop_step_number_val},
        )
        return {"ok": True, "action": "inserted", "row_id": row_id}

    except Exception as e:
        logger.error(
            "sop_step_persistence_failed",
            extra={"error": str(e), "execution_id": execution_id, "step": sop_step_number_val},
            exc_info=True,
        )
        return {"ok": False, "skipped": False, "reason": str(e)}
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Structured tool for LangGraph Supervisor
# ---------------------------------------------------------------------------
save_sop_step = StructuredTool.from_function(
    func=save_sop_step_func,
    name="save_sop_step",
    description=(
        "Persist one SOP step execution row into SQL Server table sop_step_executions "
        "(idempotent within a single execution_id). Intended to be invoked by Supervisor."
    ),
    args_schema=SaveSopStepInput,
)
