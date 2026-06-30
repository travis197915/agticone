"""LINK ROUTING LAYER — 8 agents.

After a document is fully written, discover + classify + enqueue all
child links so the BFS loop can process them in the next iteration.

1. LinkClassifierAgent      — confirms link_type for each link
2. HTMLLinkQueueAgent       — enqueues HTML_SOP links
3. DOCXLinkQueueAgent       — enqueues DOCX links
4. XLSXLinkQueueAgent       — enqueues XLSX links
5. PDFLinkQueueAgent        — enqueues PDF links
6. UnresolvedLinkLoggerAgent— logs UNRESOLVED links to Redis
7. InternalAnchorMapperAgent— stores internal anchors for intra-doc routing
8. AccumulatedDocAppenderAgent—appends current doc summary to all_documents
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)

_EXT_FMT = {".html":"HTML_SOP",".htm":"HTML_SOP",
            ".docx":"DOCX",".doc":"DOCX",
            ".xlsx":"XLSX",".xls":"XLSX",
            ".pdf":"PDF"}


# ── 1. LinkClassifierAgent ────────────────────────────────────────────────────

def link_classifier(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Re-classifies any link whose link_type is blank or UNKNOWN."""
    links = list(state.get("links") or [])
    updated = []
    for lnk in links:
        if lnk.get("link_type") and lnk["link_type"] not in ("","UNKNOWN"):
            updated.append(lnk)
            continue
        href = lnk.get("href","")
        if not href or href == "#":
            lnk["link_type"] = "UNRESOLVED"
        elif href.startswith("#"):
            lnk["link_type"] = "INTERNAL_ANCHOR"
        else:
            ext = Path(urlparse(href).path).suffix.lower()
            lnk["link_type"] = _EXT_FMT.get(ext, "HTML_SOP")
        updated.append(lnk)
    return {"links": updated}


# ── 2. HTMLLinkQueueAgent ─────────────────────────────────────────────────────

def html_link_queue(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    return _enqueue_by_type(state, "HTML_SOP")


# ── 3. DOCXLinkQueueAgent ─────────────────────────────────────────────────────

def docx_link_queue(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    return _enqueue_by_type(state, "DOCX")


# ── 4. XLSXLinkQueueAgent ─────────────────────────────────────────────────────

def xlsx_link_queue(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    return _enqueue_by_type(state, "XLSX")


# ── 5. PDFLinkQueueAgent ──────────────────────────────────────────────────────

def pdf_link_queue(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    return _enqueue_by_type(state, "PDF")


def _enqueue_by_type(state, link_type: str) -> dict:
    visited_urls = set(state.get("visited_urls") or [])
    current_depth = state.get("current_depth", 0)
    max_depth     = state.get("max_depth", 4)
    new_items = []
    for lnk in (state.get("links") or []):
        if lnk.get("link_type") != link_type: continue
        url = lnk.get("resolved_url") or lnk.get("href","")
        if not url or url in visited_urls: continue
        new_depth = current_depth + 1
        if new_depth > max_depth: continue
        new_items.append({
            "url": url,
            "depth": new_depth,
            "parent_url": state.get("current_url",""),
            "link_text": lnk.get("text",""),
            "link_type": link_type,
        })
    if new_items:
        logger.info("link_queue: enqueuing %d %s links", len(new_items), link_type)
    return {"url_queue": new_items}   # Annotated[list, operator.add] — appends


# ── 6. UnresolvedLinkLoggerAgent ──────────────────────────────────────────────

def unresolved_link_logger(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Logs UNRESOLVED (href='#') links to Redis for later resolution."""
    unresolved = [l for l in (state.get("links") or [])
                  if l.get("link_type") == "UNRESOLVED"]
    if not unresolved: return {}
    try:
        from ..config import get_redis
        r = get_redis(cfg)
        key = f"sop:job:{state.get('job_id','')}:unresolved"
        for lnk in unresolved:
            r.sadd(key, json.dumps({
                "text": lnk.get("text",""),
                "parent": state.get("current_url",""),
                "hint": " ".join(lnk.get("text","").split()[:6]),
            }))
        r.expire(key, 86400 * 7)
        logger.info("unresolved_link_logger: logged %d unresolved links", len(unresolved))
    except Exception as e:
        logger.warning("unresolved_link_logger: %s", e)
    return {}


# ── 7. InternalAnchorMapperAgent ─────────────────────────────────────────────

def internal_anchor_mapper(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Stores internal #anchor links → step/section mapping in Redis."""
    anchors = [l for l in (state.get("links") or [])
               if l.get("link_type") == "INTERNAL_ANCHOR"]
    if not anchors: return {}
    try:
        from ..config import get_redis
        r = get_redis(cfg)
        key = f"sop:anchors:{state.get('content_hash','')}"
        mapping = {lnk.get("href","").split("#")[-1]: lnk.get("text","") for lnk in anchors}
        r.hset(key, mapping=mapping)
        r.expire(key, 3600)
    except Exception as e:
        logger.warning("internal_anchor_mapper: %s", e)
    return {}


# ── 8. AccumulatedDocAppenderAgent ────────────────────────────────────────────

def accumulated_doc_appender(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Appends a summary of the current document to all_documents."""
    meta = state.get("metadata") or {}
    return {
        "all_documents": [{
            "url":            state.get("current_url",""),
            "content_hash":   state.get("content_hash",""),
            "doc_format":     state.get("doc_format",""),
            "title":          meta.get("title",""),
            "revision_date":  meta.get("revision_date",""),
            "crawl_depth":    state.get("current_depth",0),
            "step_count":     len(state.get("steps") or []),
            "rule_count":     sum(len(s.get("decision_rows",[])) for s in (state.get("steps") or [])),
            "code_count":     len(state.get("detected_codes") or []),
            "neo4j_sop_id":   state.get("neo4j_sop_id",""),
            "postgres_doc_id": state.get("sop_db_id") or state.get("postgres_doc_id"),
            "version_action": state.get("version_action",""),
            "version_diff_id": state.get("version_diff_id"),
        }]
    }
