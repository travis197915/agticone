"""INTAKE LAYER — 5 agents.

1. URLValidatorAgent      — checks the URL is non-empty and reachable scheme
2. URLNormalizerAgent     — strips whitespace, adds https:// if missing scheme
3. JobInitializerAgent    — creates a UUID job_id + sets defaults
4. RedisJobTrackerAgent   — registers job in Redis HASH
5. MongoJobLoggerAgent    — inserts job document into MongoDB
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig


# ── 1. URLValidatorAgent ──────────────────────────────────────────────────────

def url_validator(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Validates seed_url is non-empty with a recognised scheme."""
    url = (state.get("seed_url") or "").strip()
    if not url:
        return {"errors": [{"agent": "URLValidatorAgent", "msg": "seed_url is empty"}]}

    if not re.match(r"^(https?://|file://|/)", url):
        # Treat bare paths as local files
        if not url.startswith("/"):
            url = "https://" + url

    return {"seed_url": url}


# ── 2. URLNormalizerAgent ─────────────────────────────────────────────────────

def url_normalizer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Strips trailing whitespace, lowercases scheme, removes fragment."""
    url = state.get("seed_url", "")
    # Remove fragment (#anchor) from root URL — fragments are internal, not docs
    url = url.split("#")[0].rstrip("/") if "#" in url else url.rstrip("/")
    return {"seed_url": url}


# ── 3. JobInitializerAgent ────────────────────────────────────────────────────

def job_initializer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Creates a job_id and seeds the BFS queue with the root URL."""
    job_id = state.get("job_id") or str(uuid.uuid4())
    seed = state["seed_url"]
    return {
        "job_id": job_id,
        "url_queue": [{"url": seed, "depth": 0, "parent_url": "", "link_text": "root", "link_type": "HTML_SOP"}],
        "visited_urls": [],
        "visited_hashes": [],
        "all_documents": [],
        "errors": [],
        "validation_warnings": [],
        "total_processed": 0,
        "processing_complete": False,
        "max_depth": state.get("max_depth") or cfg.max_depth,
        "max_docs": state.get("max_docs") or cfg.max_docs,
    }


# ── 4. RedisJobTrackerAgent ───────────────────────────────────────────────────

def redis_job_tracker(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Registers the job in Redis as a HASH for live progress tracking."""
    try:
        from ..config import get_redis
        r = get_redis(cfg)
        key = f"sop:job:{state['job_id']}"
        r.hset(key, mapping={
            "job_id":    state["job_id"],
            "seed_url":  state["seed_url"],
            "status":    "RUNNING",
            "started_at": str(time.time()),
            "total_processed": "0",
            "total_queued": "1",
        })
        r.expire(key, 86400 * 7)  # 7-day TTL
    except Exception as e:
        return {"errors": [{"agent": "RedisJobTrackerAgent", "msg": str(e)}]}
    return {}


# ── 5. MongoJobLoggerAgent ────────────────────────────────────────────────────

def mongo_job_logger(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Inserts the job document into MongoDB for durable audit logging."""
    try:
        from ..config import get_mongo
        db = get_mongo(cfg)[cfg.mongo_database]
        db["ingestion_jobs"].insert_one({
            "_id": state["job_id"],
            "seed_url": state["seed_url"],
            "status": "RUNNING",
            "started_at": time.time(),
            "max_depth": state.get("max_depth"),
            "max_docs": state.get("max_docs"),
        })
    except Exception as e:
        return {"errors": [{"agent": "MongoJobLoggerAgent", "msg": str(e)}]}
    return {}
