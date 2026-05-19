"""PDF PARSE LAYER — 3 agents.

1. PDFTextExtractorAgent   — page-by-page text via pypdf
2. PDFMetadataAgent        — XMP / Info metadata → title, dates
3. PDFRawTextNormalizerAgent — collapses whitespace, flags low-quality pages
"""
from __future__ import annotations

import base64
import io
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)


def _reader(state):
    from pypdf import PdfReader
    b64 = state.get("raw_bytes_b64","")
    if not b64: return None
    return PdfReader(io.BytesIO(base64.b64decode(b64)))


# ── 1. PDFTextExtractorAgent ──────────────────────────────────────────────────

def pdf_text_extractor(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    reader = _reader(state)
    if not reader: return {}
    sections, warnings = [], []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if not text.strip():
            warnings.append(f"PDF page {i+1}: no extractable text (may be scanned image)")
            continue
        sections.append({
            "name": f"Page {i+1}",
            "order": i,
            "section_id": "",
            "items": [{"text": p.strip(), "item_type": "RULE", "codes": [], "sub_items": []}
                      for p in text.split("\n") if p.strip()],
            "annotations": [],
        })
    raw_text = "\n".join(
        p.strip() for s in sections for item in s["items"] for p in [item["text"]] if p.strip()
    )
    return {"pre_sections": sections, "raw_text": raw_text, "parse_warnings": warnings}


# ── 2. PDFMetadataAgent ───────────────────────────────────────────────────────

def pdf_metadata(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    reader = _reader(state)
    if not reader: return {}
    info = reader.metadata or {}
    url  = state.get("current_url","")
    meta = dict(state.get("metadata") or {})
    meta["title"] = str(info.get("/Title","") or url.rsplit("/",1)[-1].rsplit(".",1)[0])
    if info.get("/CreationDate"):
        meta["effective_date"] = str(info["/CreationDate"])[:10]
    if info.get("/ModDate"):
        meta["revision_date"] = str(info["/ModDate"])[:10]
    return {"metadata": meta}


# ── 3. PDFRawTextNormalizerAgent ─────────────────────────────────────────────

def pdf_raw_text_normalizer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    raw = state.get("raw_text","")
    if not raw: return {}
    # Collapse multiple blank lines, normalise whitespace
    normalized = re.sub(r"\n{3,}", "\n\n", raw)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    warnings = list(state.get("parse_warnings") or [])
    if len(raw) < 500:
        warnings.append("PDF: very short extracted text — possible scanned document")
    return {"raw_text": normalized.strip(), "parse_warnings": warnings}
