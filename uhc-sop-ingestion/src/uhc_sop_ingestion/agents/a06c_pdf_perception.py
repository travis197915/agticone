"""a06c_pdf_perception.py — Native-PDF perception layer (Phase 1 of the PDF door).

This is the first of three PDF-only stages that REPLACE the old pypdf text army.
It reads EVERY page of the PDF through Claude's native document API so the model
sees the true page layout, tables, redaction boxes and footnotes — exactly like
the Claude chat UI — then stitches the per-page perception into one ordered,
cross-page-coherent document. There is no text scraping and no regex/template
section detection anywhere in this layer.

Agents (each ``fn(state, cfg) -> dict`` partial-state update):
  1. ``pdf_slicer``            — split the PDF into <=N-page / <=~30MB base64
                                 slices (pypdf) with a 1-page overlap so a table
                                 spanning a slice boundary is still seen whole.
  2. ``pdf_page_reader``       — per slice: native PDF -> structured per-page JSON
                                 (blocks, tables with rows+cells, codes, warnings,
                                 ``continues_*`` flags). Persisted to Redis (the
                                 per-job working blackboard) + Mongo (raw audit
                                 trail) so nothing is ever lost and re-runs are
                                 possible without re-calling the LLM.
  3. ``pdf_perception_merger`` — merge slices into one ordered page list, drop the
                                 duplicated overlap pages, and resolve tables/rows
                                 that continue across page breaks.

Context store roles (per the agreed design):
  * Redis  — ephemeral per-job blackboard, namespace ``sop:pdf:{job_id}:*``.
  * Mongo  — durable raw per-page perception (collection ``pdf_perception``).
  * Neo4j  — durable context graph (built in a06d).
  * Postgres — final canonical write (unchanged, downstream).
"""
from __future__ import annotations

import base64
import io
import json
import logging
from typing import TYPE_CHECKING, Any

from .a07_enrich import _llm_call_pdf

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)

_CONTEXT_TTL_SECONDS = 24 * 3600
_MAX_SLICE_BYTES = 30 * 1024 * 1024  # stay safely under Claude's 32MB doc cap


# ── Redis blackboard (sop:pdf namespace) — shared with a06d / a06e ────────────

def _redis(cfg: "PipelineConfig"):
    from ..config import get_redis
    return get_redis(cfg)


def _pdf_key(job_id: str, section: str) -> str:
    return f"sop:pdf:{job_id}:{section}"


def ctx_write(cfg, job_id: str, section: str, payload: Any) -> None:
    """Persist an agent's output to the shared per-job PDF blackboard."""
    try:
        r = _redis(cfg)
        key = _pdf_key(job_id, section)
        r.set(key, json.dumps(payload, default=str))
        r.expire(key, _CONTEXT_TTL_SECONDS)
    except Exception as exc:
        logger.warning("pdf ctx_write[%s] failed: %s", section, exc)


def ctx_read(cfg, job_id: str, section: str, default=None):
    try:
        r = _redis(cfg)
        raw = r.get(_pdf_key(job_id, section))
        return json.loads(raw) if raw else default
    except Exception as exc:
        logger.warning("pdf ctx_read[%s] failed: %s", section, exc)
        return default


def _mongo_persist_perception(cfg, job_id: str, content_hash: str,
                              url: str, pages: list[dict]) -> None:
    """Durable raw audit trail of the per-page perception (idempotent upsert)."""
    try:
        from ..config import get_mongo
        client = get_mongo(cfg)
        coll = client[cfg.mongo_database]["pdf_perception"]
        coll.update_one(
            {"job_id": job_id, "content_hash": content_hash},
            {"$set": {
                "job_id": job_id,
                "content_hash": content_hash,
                "url": url,
                "page_count": len(pages),
                "pages": pages,
            }},
            upsert=True,
        )
    except Exception as exc:
        logger.warning("pdf perception mongo persist failed: %s", exc)


# ── 1. pdf_slicer ─────────────────────────────────────────────────────────────

def _encode_pages(reader, start: int, end: int) -> str:
    """Build a base64 PDF containing global pages [start, end) (0-indexed)."""
    from pypdf import PdfWriter
    writer = PdfWriter()
    for p in range(start, end):
        writer.add_page(reader.pages[p])
    buf = io.BytesIO()
    writer.write(buf)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _build_slices(reader, slice_pages: int, overlap: int) -> list[dict]:
    """Window the PDF into base64 slices, recursively halving any slice that
    exceeds the document-size cap. Each slice records the GLOBAL page range it
    covers (1-indexed, inclusive) so the reader can label pages correctly and
    the merger can drop the duplicated overlap pages.
    """
    total = len(reader.pages)
    slices: list[dict] = []

    def _emit(start: int, end: int) -> None:
        # start/end are 0-indexed, end exclusive
        if start >= end:
            return
        b64 = _encode_pages(reader, start, end)
        if len(b64) > _MAX_SLICE_BYTES and (end - start) > 1:
            mid = start + (end - start) // 2
            _emit(start, mid)
            _emit(mid, end)
            return
        slices.append({
            "slice_index": len(slices),
            "page_start": start + 1,   # global, 1-indexed inclusive
            "page_end": end,           # global, 1-indexed inclusive (== end-1+1)
            "pdf_b64": b64,
        })

    start = 0
    while start < total:
        end = min(start + slice_pages, total)
        _emit(start, end)
        if end >= total:
            break
        start = end - overlap if overlap < (end - start) else end
    return slices


def pdf_slicer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    b64 = state.get("raw_bytes_b64", "")
    if not b64:
        return {}
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(base64.b64decode(b64)))
    except Exception as exc:
        logger.warning("pdf_slicer: cannot open PDF: %s", exc)
        return {}

    slice_pages = max(1, getattr(cfg, "pdf_slice_pages", 20))
    overlap = max(0, getattr(cfg, "pdf_slice_overlap", 1))
    slices = _build_slices(reader, slice_pages, overlap)
    page_count = len(reader.pages)

    job_id = state.get("job_id", "")
    # Keep the heavy base64 slices on the Redis blackboard, NOT in LangGraph
    # state, so they never bloat the master state or leak across BFS documents.
    ctx_write(cfg, job_id, "slices", slices)

    logger.info("pdf_slicer: %d pages -> %d slices (slice_pages=%d overlap=%d)",
                page_count, len(slices), slice_pages, overlap)
    return {"pdf_page_count": page_count, "pdf_slice_count": len(slices)}


# ── 2. pdf_page_reader ────────────────────────────────────────────────────────

_PERCEPTION_SCHEMA_HINT = json.dumps({
    "pages": [{
        "page_number": "int — GLOBAL page number (use the offset stated below)",
        "continues_from_prev_page": "bool — true if this page's first content "
                                    "is a continuation of the previous page",
        "blocks": [{
            "type": "heading | paragraph | step | list_item | note | warning | footer | caption",
            "text": "verbatim text — copy EVERY word, do not summarise",
            "level": "int — heading/outline depth when applicable, else 0",
        }],
        "tables": [{
            "title": "str — nearby caption/header, else ''",
            "columns": ["verbatim column header text"],
            "continues_from_prev_page": "bool",
            "continues_to_next_page": "bool",
            "rows": [{
                "cells": ["verbatim cell text, one per column"],
                "continues_from_prev_row": "bool — true if this row wraps the "
                                           "previous row rather than starting a new one",
            }],
        }],
        "codes": ["any code-like token verbatim (EOB/EX/denial/CPT/POS/etc.)"],
    }],
}, indent=2)


def _read_one_slice(cfg, slc: dict) -> list[dict]:
    """Native-PDF perception of a single slice. Returns its list of page dicts."""
    p_start = slc["page_start"]
    p_end = slc["page_end"]
    prompt = f"""You are a meticulous document-perception engine for claims-audit
SOPs. The attached PDF is a SLICE of a larger document covering GLOBAL pages
{p_start} to {p_end}. The slice's first physical page is global page {p_start}.

Read EVERY page completely. Transcribe every word, table cell, footnote, warning
and code VERBATIM — this is a compliance document where a single missed letter is
a defect. Do not summarise, do not paraphrase, do not skip boilerplate.

For each page return its structured content. Preserve table structure exactly:
one entry per row, one cell per column, in reading order. Flag any table or row
that visually continues from the previous page/row so cross-page rows can be
re-joined later. Set page_number to the GLOBAL page number.

Return STRICT JSON matching this contract:
{_PERCEPTION_SCHEMA_HINT}
"""
    result = _llm_call_pdf(
        cfg, prompt, [slc["pdf_b64"]],
        fallback={"pages": []},
        agent_name="pdf_page_reader",
        expected_type=dict,
        required_keys=["pages"],
        stage="pdf_perceive",
        max_tokens=16384,
    )
    pages = result.get("pages") if isinstance(result, dict) else None
    return pages or []


def pdf_page_reader(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    slices = ctx_read(cfg, job_id, "slices", default=[]) or []
    if not slices:
        return {}

    per_slice: list[dict] = []
    for slc in slices:
        pages = _read_one_slice(cfg, slc)
        per_slice.append({
            "slice_index": slc["slice_index"],
            "page_start": slc["page_start"],
            "page_end": slc["page_end"],
            "pages": pages,
        })

    ctx_write(cfg, job_id, "slice_perception", per_slice)
    total_pages = sum(len(s["pages"]) for s in per_slice)
    logger.info("pdf_page_reader: perceived %d page-records across %d slices",
                total_pages, len(per_slice))
    return {}


# ── 3. pdf_perception_merger ──────────────────────────────────────────────────

def _merge_continued_tables(pages: list[dict]) -> list[dict]:
    """Re-join tables/rows split across a page break.

    When a page's first table is flagged ``continues_from_prev_page`` (or its
    first row is a continuation), append its rows onto the previous page's last
    table instead of starting a new one. Conservative: only merges when the
    previous page actually ended with a table.
    """
    for i in range(1, len(pages)):
        cur = pages[i]
        prev = pages[i - 1]
        cur_tables = cur.get("tables") or []
        prev_tables = prev.get("tables") or []
        if not cur_tables or not prev_tables:
            continue
        first = cur_tables[0]
        if first.get("continues_from_prev_page") or (
            (first.get("rows") or [{}])[0].get("continues_from_prev_row")
        ):
            prev_last = prev_tables[-1]
            prev_last.setdefault("rows", [])
            prev_last["rows"].extend(first.get("rows") or [])
            prev_last["continues_to_next_page"] = first.get("continues_to_next_page", False)
            cur["tables"] = cur_tables[1:]
    return pages


def _reread_pages(cfg, state, page_numbers: list[int]) -> list[dict]:
    """Re-perceive specific GLOBAL pages individually (1-page slices).

    The completeness floor: if a multi-page slice ever returns fewer pages than
    it contained, every missing page is read again on its own so a compliance
    SOP can never silently lose a page. 1-page slices cannot truncate.
    """
    b64 = state.get("raw_bytes_b64", "")
    if not b64 or not page_numbers:
        return []
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(base64.b64decode(b64)))
    except Exception as exc:
        logger.warning("_reread_pages: cannot open PDF: %s", exc)
        return []
    out: list[dict] = []
    for n in page_numbers:
        if n < 1 or n > len(reader.pages):
            continue
        slc = {
            "slice_index": -n,
            "page_start": n,
            "page_end": n,
            "pdf_b64": _encode_pages(reader, n - 1, n),
        }
        out.extend(_read_one_slice(cfg, slc))
    return out


def pdf_perception_merger(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    per_slice = ctx_read(cfg, job_id, "slice_perception", default=[]) or []
    if not per_slice:
        return {}

    # Flatten slices into a single page map keyed by global page_number. Because
    # slices overlap by `overlap` pages, the same global page can appear twice;
    # we keep the FIRST occurrence (earlier slice saw it with more leading
    # context) and ignore duplicates so nothing is double-counted.
    by_page: dict[int, dict] = {}
    fallback_order = 0
    for s in sorted(per_slice, key=lambda x: x.get("slice_index", 0)):
        for pg in s.get("pages") or []:
            num = pg.get("page_number")
            if not isinstance(num, int):
                fallback_order += 1
                num = 10_000 + fallback_order  # keep unlabeled pages, ordered last
                pg["page_number"] = num
            by_page.setdefault(num, pg)

    # Completeness floor — every global page 1..N must be present. Any page a
    # multi-page slice failed to return is re-read on its own (1-page slices
    # cannot truncate), so no page is ever silently dropped.
    total = state.get("pdf_page_count", 0) or 0
    if total:
        missing = [n for n in range(1, total + 1) if n not in by_page]
        if missing:
            logger.warning("pdf_perception_merger: %d page(s) missing after slicing: "
                           "%s — re-reading individually", len(missing), missing)
            for pg in _reread_pages(cfg, state, missing):
                num = pg.get("page_number")
                if isinstance(num, int):
                    by_page.setdefault(num, pg)

    pages = [by_page[n] for n in sorted(by_page)]
    pages = _merge_continued_tables(pages)

    ctx_write(cfg, job_id, "pages", pages)
    _mongo_persist_perception(
        cfg, job_id,
        state.get("content_hash", ""),
        state.get("current_url", ""),
        pages,
    )

    logger.info("pdf_perception_merger: merged into %d ordered pages", len(pages))
    # Light reference only — the page payload lives on the Redis blackboard.
    return {"pdf_page_count": len(pages)}
