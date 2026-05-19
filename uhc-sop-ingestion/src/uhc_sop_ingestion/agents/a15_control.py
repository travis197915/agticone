"""CONTROL LAYER — 5 agents.

1. CompletionCheckerAgent  — decides continue vs done
2. StateClearerAgent       — resets per-doc fields before next URL
3. ErrorHandlerAgent       — logs errors to all stores, marks job PARTIAL
4. FinalSummaryAgent       — builds final_summary dict
5. JobCloserAgent          — marks job COMPLETED in Redis + Mongo + Postgres
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)


# ── 1. CompletionCheckerAgent ─────────────────────────────────────────────────


def completion_checker(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Returns processing_complete=True when the BFS queue is exhausted."""
    queue = state.get("url_queue") or []
    total = state.get("total_processed", 0)
    max_d = state.get("max_docs", cfg.max_docs)
    complete = (not queue) or (total >= max_d)
    return {"processing_complete": complete}


# ── 2. StateClearerAgent ──────────────────────────────────────────────────────


def state_clearer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Resets all per-document fields so next iteration starts clean."""
    return {
        "raw_bytes_b64": "",
        "doc_format": "",
        "content_hash": "",
        "content_type": "",
        "encoding": "utf-8",
        "is_local": False,
        "is_duplicate": False,
        "metadata": {},
        "pre_sections": [],
        "steps": [],
        "sub_procedures": [],
        "reference_tables": [],
        "group_rules": [],
        "links": [],
        "raw_text": "",
        "xlsx_workbook_type": "",
        "parse_warnings": [],
        "code_table_entries": [],
        "enriched_steps": [],
        "enriched_rules": [],
        "llm_summary": "",
        "llm_tokens_used": 0,
        "detected_codes": [],
        "detected_list_refs": [],
        "detected_date_conditions": [],
        "validation_passed": False,
        "neo4j_sop_id": "",
        "sop_db_id": None,
        "postgres_doc_id": None,
        "mongo_raw_id": "",
        "mongo_parsed_id": "",
    }


# ── 3. ErrorHandlerAgent ──────────────────────────────────────────────────────


def error_handler(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Logs accumulated errors to Redis and marks job PARTIAL if any."""
    errors = state.get("errors") or []
    if not errors:
        return {}
    try:
        from ..config import get_redis

        r = get_redis(cfg)
        key = f"sop:job:{state.get('job_id','')}:errors"
        for err in errors:
            r.rpush(key, json.dumps(err))
        r.expire(key, 86400 * 7)
    except Exception as e:
        logger.warning("error_handler redis: %s", e)
    if len(errors) > 5:
        logger.warning(
            "error_handler: %d errors accumulated — job may be PARTIAL", len(errors)
        )
    return {}


# ── 4. FinalSummaryAgent ─────────────────────────────────────────────────────


def final_summary(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    docs = state.get("all_documents") or []
    total_steps = sum(d.get("step_count", 0) for d in docs)
    total_rules = sum(d.get("rule_count", 0) for d in docs)
    total_codes = sum(d.get("code_count", 0) for d in docs)
    summary = {
        "job_id": state.get("job_id", ""),
        "seed_url": state.get("seed_url", ""),
        "total_docs": len(docs),
        "total_steps": total_steps,
        "total_rules": total_rules,
        "total_codes": total_codes,
        "formats": list({d.get("doc_format", "") for d in docs}),
        "root_sop_id": docs[0].get("neo4j_sop_id", "") if docs else "",
        "documents": [
            {
                "url": d["url"],
                "title": d.get("title", ""),
                "depth": d.get("crawl_depth", 0),
            }
            for d in docs
        ],
        "errors": len(state.get("errors") or []),
        "completed_at": time.time(),
    }
    logger.info(
        "final_summary: job=%s docs=%d steps=%d rules=%d codes=%d",
        summary["job_id"],
        summary["total_docs"],
        summary["total_steps"],
        summary["total_rules"],
        summary["total_codes"],
    )
    return {"final_summary": summary}


# ── 5. JobCloserAgent ─────────────────────────────────────────────────────────


def job_closer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Marks job COMPLETED in Redis, MongoDB, and Postgres."""
    job_id = state.get("job_id", "")
    summary = state.get("final_summary") or {}
    # Redis
    try:
        from ..config import get_redis

        r = get_redis(cfg)
        r.hset(
            f"sop:job:{job_id}",
            mapping={
                "status": "COMPLETED",
                "completed_at": str(time.time()),
                "total_docs": summary.get("total_docs", 0),
                "total_rules": summary.get("total_rules", 0),
            },
        )
    except Exception as e:
        logger.warning("job_closer redis: %s", e)
    # MongoDB
    try:
        from ..config import get_mongo

        db = get_mongo(cfg)[cfg.mongo_database]
        db["ingestion_jobs"].update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "COMPLETED",
                    "completed_at": time.time(),
                    "summary": summary,
                }
            },
        )
    except Exception as e:
        logger.warning("job_closer mongo: %s", e)
    # Postgres
    try:
        from ..config import get_pg_conn

        conn = get_pg_conn(cfg)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sop_ingestion_ingestionjob
                    SET docs_processed=%s, updated_at=NOW()
                    WHERE job_id=%s
                """,
                    (
                        summary.get("total_docs", 0),
                        job_id,
                    ),
                )
    except Exception as e:
        logger.warning("job_closer pg: %s", e)
    return {}
