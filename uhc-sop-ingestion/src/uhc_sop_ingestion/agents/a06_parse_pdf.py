"""PDF METADATA — 1 agent.

The old pypdf TEXT-extraction path (``pdf_text_extractor``,
``pdf_raw_text_normalizer``, ``pdf_step_inventory`` + the regex section
segmentation) has been RETIRED in favour of the native-PDF vision door
(``a06c_pdf_perception`` → ``a06d_pdf_context_graph`` → ``a06e_pdf_synthesis``),
which reads every page through Claude's document API instead of scraping text.

This module now only provides lightweight document metadata (title / dates) read
straight from the PDF's Info dictionary — no content extraction.
"""
from __future__ import annotations

import base64
import io
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)


def _reader(state):
    from pypdf import PdfReader
    b64 = state.get("raw_bytes_b64", "")
    if not b64:
        return None
    return PdfReader(io.BytesIO(base64.b64decode(b64)))


def pdf_metadata(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    reader = _reader(state)
    if not reader:
        return {}
    info = reader.metadata or {}
    url = state.get("current_url", "")
    meta = dict(state.get("metadata") or {})
    meta["title"] = str(info.get("/Title", "") or url.rsplit("/", 1)[-1].rsplit(".", 1)[0])
    if info.get("/CreationDate"):
        meta["effective_date"] = str(info["/CreationDate"])[:10]
    if info.get("/ModDate"):
        meta["revision_date"] = str(info["/ModDate"])[:10]
    return {"metadata": meta}
