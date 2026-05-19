"""MONGODB WRITE LAYER — 3 agents.

MongoDB stores two documents per ingested SOP:
  1. RawDocumentStorageAgent   — full raw bytes + fetch metadata
  2. ParsedDocumentStorageAgent— all structured parse output
  3. MongoJobProgressAgent     — updates the job document

Using MongoDB for raw + JSON gives schema flexibility: new fields
(future code systems, parser upgrades) don't require migrations.
"""
from __future__ import annotations

import base64
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)


def _db(cfg):
    from ..config import get_mongo
    return get_mongo(cfg)[cfg.mongo_database]


# ── 1. RawDocumentStorageAgent ────────────────────────────────────────────────

def mongo_raw_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Stores raw bytes, content hash, URL, fetch metadata."""
    try:
        db = _db(cfg)
        doc_id = f"{state.get('job_id','')}:{state.get('content_hash','')}"
        result = db["raw_documents"].replace_one(
            {"_id": doc_id},
            {
                "_id": doc_id,
                "job_id": state.get("job_id",""),
                "url": state.get("current_url",""),
                "content_hash": state.get("content_hash",""),
                "doc_format": state.get("doc_format",""),
                "content_type": state.get("content_type",""),
                "encoding": state.get("encoding","utf-8"),
                "crawl_depth": state.get("current_depth",0),
                "parent_url": state.get("current_parent_url",""),
                "raw_bytes_b64": state.get("raw_bytes_b64",""),
                "ingested_at": time.time(),
            },
            upsert=True,
        )
        return {"mongo_raw_id": doc_id}
    except Exception as e:
        logger.error("mongo_raw_writer: %s", e)
        return {"errors": [{"agent": "RawDocumentStorageAgent", "msg": str(e)}]}


# ── 2. ParsedDocumentStorageAgent ────────────────────────────────────────────

def mongo_parsed_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Stores the full structured parse output — schema-free JSON."""
    try:
        db = _db(cfg)
        doc_id = f"parsed:{state.get('job_id','')}:{state.get('content_hash','')}"
        result = db["parsed_documents"].replace_one(
            {"_id": doc_id},
            {
                "_id": doc_id,
                "job_id": state.get("job_id",""),
                "url": state.get("current_url",""),
                "content_hash": state.get("content_hash",""),
                "doc_format": state.get("doc_format",""),
                "neo4j_sop_id": state.get("neo4j_sop_id",""),
                "postgres_doc_id": state.get("postgres_doc_id"),
                "metadata": state.get("metadata") or {},
                "pre_sections": state.get("pre_sections") or [],
                "steps": state.get("enriched_steps") or state.get("steps") or [],
                "sub_procedures": state.get("sub_procedures") or [],
                "reference_tables": state.get("reference_tables") or [],
                "group_rules": state.get("group_rules") or [],
                "links": state.get("links") or [],
                "detected_codes": state.get("detected_codes") or [],
                "detected_list_refs": state.get("detected_list_refs") or [],
                "detected_date_conditions": state.get("detected_date_conditions") or [],
                "code_table_entries": state.get("code_table_entries") or [],
                "llm_summary": state.get("llm_summary",""),
                "validation_passed": state.get("validation_passed",False),
                "validation_warnings": state.get("validation_warnings") or [],
                "parse_warnings": state.get("parse_warnings") or [],
                "parsed_at": time.time(),
            },
            upsert=True,
        )
        return {"mongo_parsed_id": doc_id}
    except Exception as e:
        logger.error("mongo_parsed_writer: %s", e)
        return {"errors": [{"agent": "ParsedDocumentStorageAgent", "msg": str(e)}]}


# ── 3. MongoJobProgressAgent ─────────────────────────────────────────────────

def mongo_job_progress(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Increments counters on the job document in MongoDB."""
    try:
        db = _db(cfg)
        db["ingestion_jobs"].update_one(
            {"_id": state.get("job_id","")},
            {"$inc": {
                "total_processed": 1,
                "total_rules": sum(len(s.get("decision_rows",[])) for s in
                                   (state.get("enriched_steps") or state.get("steps") or [])),
                "total_codes": len(state.get("detected_codes") or []),
            }, "$set": {"last_url": state.get("current_url",""), "updated_at": time.time()}},
        )
    except Exception as e:
        logger.warning("mongo_job_progress: %s", e)
    return {}
