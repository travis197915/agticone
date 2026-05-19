"""Real-time PostgreSQL logger for the SOP ingestion pipeline.

Writes directly via psycopg2 (no Django ORM dependency) so it works
inside the uhc-sop-ingestion package. The rows land in the same tables
that the Django models (`PipelineStageLog`, `LLMCallLog`) point to.

Usage (attached to cfg by SopIngestionPipeline.run):
    cfg._pg_logger = PipelineLogger(cfg, job_id)
    ...
    cfg._pg_logger.close()
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Table names must match Django's app_label + model_name convention
_STAGE_TABLE = "sop_ingestion_pipelinestagelog"
_LLM_TABLE   = "sop_ingestion_llmcalllog"
_JOB_TABLE   = "sop_ingestion_ingestionjob"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PipelineLogger:
    """Writes pipeline events to Postgres in real time using autocommit."""

    def __init__(self, cfg, job_id: str) -> None:
        self.cfg    = cfg
        self.job_id = job_id
        self._conn  = None

    # ── Connection management ─────────────────────────────────────────────────

    def _get_conn(self):
        if self._conn is None or self._conn.closed:
            try:
                import psycopg2
                self._conn = psycopg2.connect(self.cfg.pg_dsn)
                self._conn.autocommit = True
            except Exception as exc:
                logger.warning("PipelineLogger: cannot connect to PG: %s", exc)
                self._conn = None
        return self._conn

    def _exec(self, sql: str, params: tuple) -> int | None:
        """Execute SQL, return lastrowid. Silent on error."""
        conn = self._get_conn()
        if not conn:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()[0] if cur.description else None
        except Exception as exc:
            logger.warning("PipelineLogger._exec error: %s", exc)
            return None

    # ── Stage logging ─────────────────────────────────────────────────────────

    def log_stage_start(
        self,
        stage_name: str,
        doc_url: str = "",
        doc_format: str = "",
        doc_depth: int | None = None,
    ) -> int | None:
        """Insert a stage-start row. Returns the PK to pass to log_stage_end."""
        sql = f"""
            INSERT INTO {_STAGE_TABLE}
                (job_id, stage_name, doc_url, doc_format, doc_depth,
                 started_at, status, error_detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        return self._exec(sql, (
            self.job_id, stage_name,
            doc_url or "", doc_format or "", doc_depth,
            _now_iso(), "OK", "",
        ))

    def log_stage_end(
        self,
        row_id: int | None,
        status: str = "OK",
        error_detail: str = "",
        started_ts: float | None = None,
    ) -> None:
        """Update the stage row with completion time, duration, and status."""
        if row_id is None:
            return
        now = _now_iso()
        duration_ms: int | None = None
        if started_ts is not None:
            duration_ms = int((time.time() - started_ts) * 1000)
        sql = f"""
            UPDATE {_STAGE_TABLE}
               SET completed_at = %s,
                   duration_ms  = %s,
                   status       = %s,
                   error_detail = %s
             WHERE id = %s
        """
        self._exec(sql, (now, duration_ms, status, error_detail or "", row_id))

    # ── LLM call logging ──────────────────────────────────────────────────────

    def log_llm_call(
        self,
        agent_name: str,
        stage: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        duration_ms: int,
        success: bool = True,
        error_message: str = "",
    ) -> None:
        total = prompt_tokens + completion_tokens
        sql = f"""
            INSERT INTO {_LLM_TABLE}
                (job_id, stage, agent_name, llm_provider, llm_model,
                 prompt_tokens, completion_tokens, total_tokens,
                 duration_ms, success, error_message, called_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        self._exec(sql, (
            self.job_id, stage, agent_name, provider, model,
            prompt_tokens, completion_tokens, total,
            duration_ms, success, error_message or "", _now_iso(),
        ))

    # ── Job aggregate update ──────────────────────────────────────────────────

    def refresh_job_totals(self) -> None:
        """Recompute LLM token aggregates directly in SQL."""
        sql = f"""
            UPDATE {_JOB_TABLE}
               SET total_llm_calls  = (SELECT COUNT(*)            FROM {_LLM_TABLE} WHERE job_id = %s),
                   total_tokens_in  = (SELECT COALESCE(SUM(prompt_tokens), 0)      FROM {_LLM_TABLE} WHERE job_id = %s),
                   total_tokens_out = (SELECT COALESCE(SUM(completion_tokens), 0)  FROM {_LLM_TABLE} WHERE job_id = %s)
             WHERE job_id = %s
        """
        self._exec(sql, (self.job_id, self.job_id, self.job_id, self.job_id))

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
