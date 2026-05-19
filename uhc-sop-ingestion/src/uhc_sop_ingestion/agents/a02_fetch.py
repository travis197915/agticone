"""FETCH LAYER — 8 agents.

1. NextURLPickerAgent      — pops next URL from the BFS queue
2. DepthLimitCheckerAgent  — skips URLs that exceed max_depth
3. HTTPFetcherAgent        — fetches remote URLs via requests
4. LocalFileFetcherAgent   — reads local file paths
5. ContentTypeDetectorAgent— reads Content-Type header
6. ExtensionDetectorAgent  — detects format from URL extension
7. MagicBytesDetectorAgent — detects format from first 16 bytes
8. ContentHasherAgent      — SHA-256[:16] of raw bytes
9. DuplicateCheckerAgent   — checks visited_hashes to skip re-processing
"""
from __future__ import annotations

import base64
import hashlib
import mimetypes
import zipfile
import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)

_EXT_FMT = {
    ".html": "HTML", ".htm": "HTML",
    ".docx": "DOCX", ".doc": "DOCX",
    ".xlsx": "XLSX", ".xls": "XLSX",
    ".pdf":  "PDF",
}
_MIME_FMT = {
    "text/html": "HTML",
    "application/xhtml+xml": "HTML",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
    "application/pdf": "PDF",
}


# ── 1. NextURLPickerAgent ─────────────────────────────────────────────────────

def next_url_picker(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Pops the first item from the BFS queue into current_url."""
    queue: list[dict] = list(state.get("url_queue") or [])
    visited_urls: list[str] = list(state.get("visited_urls") or [])

    # Find first unvisited URL
    while queue:
        item = queue.pop(0)
        url = item["url"]
        if url not in visited_urls:
            remaining = queue
            return {
                "url_queue": [],          # clear accumulator — replaced below
                "current_url": url,
                "current_depth": item.get("depth", 0),
                "current_parent_url": item.get("parent_url", ""),
                "processing_complete": False,
            }

    # Queue exhausted
    return {"processing_complete": True, "current_url": ""}


# ── 2. DepthLimitCheckerAgent ─────────────────────────────────────────────────

def depth_limit_checker(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Marks document as skip if depth exceeds max_depth."""
    depth = state.get("current_depth", 0)
    max_d = state.get("max_depth", cfg.max_depth)
    total = state.get("total_processed", 0)
    max_docs = state.get("max_docs", cfg.max_docs)
    if depth > max_d or total >= max_docs:
        logger.info("depth_limit_checker: skipping depth=%d total=%d", depth, total)
        return {"is_duplicate": True}   # reuse duplicate flag to skip processing
    return {"is_duplicate": False}


# ── 3. HTTPFetcherAgent ───────────────────────────────────────────────────────

def http_fetcher(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Fetches a remote URL with requests, returns base64-encoded content."""
    url = state.get("current_url", "")
    if not url.startswith(("http://", "https://")):
        return {}   # not an HTTP URL — let local_file_fetcher handle it

    import requests
    headers = {"User-Agent": "UHC-SOP-Ingestion/1.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=30, stream=True)
        resp.raise_for_status()
        chunks, total = [], 0
        for chunk in resp.iter_content(65536):
            total += len(chunk)
            if total > 100 * 1024 * 1024:
                return {"errors": [{"agent": "HTTPFetcherAgent", "msg": f"Response >100MiB: {url}"}]}
            chunks.append(chunk)
        raw = b"".join(chunks)
        return {
            "raw_bytes_b64": base64.b64encode(raw).decode(),
            "content_type": resp.headers.get("Content-Type", ""),
            "encoding": resp.encoding or "utf-8",
            "is_local": False,
        }
    except Exception as e:
        return {"errors": [{"agent": "HTTPFetcherAgent", "msg": f"{url}: {e}"}]}


# ── 4. LocalFileFetcherAgent ──────────────────────────────────────────────────

def local_file_fetcher(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Reads a local file path into raw_bytes_b64."""
    url = state.get("current_url", "")
    if url.startswith(("http://", "https://")):
        return {}   # handled by http_fetcher

    path = Path(url.replace("file://", ""))
    if not path.exists():
        return {"errors": [{"agent": "LocalFileFetcherAgent", "msg": f"Not found: {path}"}]}
    raw = path.read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or ""
    return {
        "raw_bytes_b64": base64.b64encode(raw).decode(),
        "content_type": mime,
        "encoding": "utf-8",
        "is_local": True,
    }


# ── 5. ContentTypeDetectorAgent ───────────────────────────────────────────────

def content_type_detector(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Maps Content-Type header to doc_format."""
    ct = state.get("content_type", "").split(";")[0].strip().lower()
    fmt = _MIME_FMT.get(ct, "")
    return {"doc_format": fmt} if fmt else {}


# ── 6. ExtensionDetectorAgent ─────────────────────────────────────────────────

def extension_detector(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Detects doc_format from the URL file extension (most reliable)."""
    if state.get("doc_format"):
        return {}   # already detected
    url = state.get("current_url", "")
    suffix = Path(urlparse(url).path).suffix.lower()
    fmt = _EXT_FMT.get(suffix, "")
    return {"doc_format": fmt} if fmt else {}


# ── 7. MagicBytesDetectorAgent ────────────────────────────────────────────────

def magic_bytes_detector(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Detects doc_format from the first 16 bytes (magic bytes)."""
    if state.get("doc_format") and state["doc_format"] != "UNKNOWN":
        return {}
    b64 = state.get("raw_bytes_b64", "")
    if not b64:
        return {"doc_format": "UNKNOWN"}

    raw = base64.b64decode(b64)
    head = raw[:16].lower()

    if head.startswith(b"%pdf"):
        return {"doc_format": "PDF"}
    if head[:4] == b"PK\x03\x04":
        # ZIP — could be XLSX or DOCX
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                names = z.namelist()
                fmt = "XLSX" if any(n.startswith("xl/") for n in names) else "DOCX"
            return {"doc_format": fmt}
        except Exception:
            pass
    if head.startswith((b"<!doctype", b"<html", b"<?xml")):
        return {"doc_format": "HTML"}

    return {"doc_format": "UNKNOWN"}


# ── 8. ContentHasherAgent ─────────────────────────────────────────────────────

def content_hasher(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Computes SHA-256[:16] of raw content for deduplication."""
    b64 = state.get("raw_bytes_b64", "")
    if not b64:
        return {}
    raw = base64.b64decode(b64)
    h = hashlib.sha256(raw).hexdigest()[:16]
    return {"content_hash": h}


# ── 9. DuplicateCheckerAgent ──────────────────────────────────────────────────

def duplicate_checker(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Marks is_duplicate=True if content_hash was already processed."""
    h = state.get("content_hash", "")
    visited = state.get("visited_hashes") or []
    url = state.get("current_url", "")
    visited_urls = state.get("visited_urls") or []

    is_dup = (h and h in visited) or (url and url in visited_urls)
    if is_dup:
        logger.debug("duplicate_checker: skipping hash=%s url=%s", h, url)

    # Always record this URL + hash as visited
    new_hashes = [h] if h and h not in visited else []
    new_urls   = [url] if url and url not in visited_urls else []

    return {
        "is_duplicate": bool(is_dup),
        "visited_hashes": new_hashes,
        "visited_urls": new_urls,
    }
