"""REDIS LAYER — 3 agents.

1. RedisCacheWriterAgent    — caches parsed document JSON (1h TTL)
2. RedisQueueManagerAgent   — writes BFS state to Redis for recovery
3. RedisProgressTrackerAgent— updates live job progress counters
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


def _r(cfg):
    from ..config import get_redis
    return get_redis(cfg)


# ── 1. RedisCacheWriterAgent ─────────────────────────────────────────────────

def redis_cache_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Caches the parsed document summary (without raw bytes) for 1 hour."""
    h = state.get("content_hash","")
    if not h: return {}
    try:
        r = _r(cfg)
        cache_key = f"sop:cache:{h}"
        payload = json.dumps({
            "url": state.get("current_url",""),
            "doc_format": state.get("doc_format",""),
            "title": (state.get("metadata") or {}).get("title",""),
            "step_count": len(state.get("steps") or []),
            "code_count": len(state.get("detected_codes") or []),
            "neo4j_sop_id": state.get("neo4j_sop_id",""),
            "postgres_doc_id": state.get("postgres_doc_id"),
            "cached_at": time.time(),
        })
        r.set(cache_key, payload, ex=3600)
    except Exception as e:
        logger.warning("redis_cache_writer: %s", e)
    return {}


# ── 2. RedisQueueManagerAgent ─────────────────────────────────────────────────

def redis_queue_manager(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Persists current BFS queue to Redis for crash recovery."""
    job_id = state.get("job_id","")
    if not job_id: return {}
    try:
        r = _r(cfg)
        queue_key = f"sop:job:{job_id}:queue"
        visited_key = f"sop:job:{job_id}:visited"
        queue = state.get("url_queue") or []
        r.delete(queue_key)
        if queue:
            r.rpush(queue_key, *[json.dumps(q) for q in queue])
            r.expire(queue_key, 86400)
        for h in (state.get("visited_hashes") or []):
            r.sadd(visited_key, h)
        r.expire(visited_key, 86400)
    except Exception as e:
        logger.warning("redis_queue_manager: %s", e)
    return {}


# ── 3. RedisProgressTrackerAgent ─────────────────────────────────────────────

def redis_progress_tracker(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Updates the live job progress hash in Redis."""
    job_id = state.get("job_id","")
    if not job_id: return {}
    try:
        r = _r(cfg)
        key = f"sop:job:{job_id}"
        r.hset(key, mapping={
            "total_processed": state.get("total_processed",0) + 1,
            "total_queued": len(state.get("url_queue") or []),
            "last_url": state.get("current_url",""),
            "last_updated": str(time.time()),
        })
    except Exception as e:
        logger.warning("redis_progress_tracker: %s", e)
    return {"total_processed": (state.get("total_processed") or 0) + 1}
