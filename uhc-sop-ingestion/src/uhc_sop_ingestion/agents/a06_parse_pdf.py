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


# ── 4. PDFStepInventoryAgent ─────────────────────────────────────────────────
# Deterministic step DETECTION for PDFs. This is the first stage of the
# SELF-CONTAINED PDF agentic flow (a06b.pdf_steps_agent does the LLM extraction)
# and is intentionally kept SEPARATE from the HTML step/reconciler path — it
# writes the PDF-private ``pdf_inventory`` key, never the HTML ``step_inventory``.
#
# Output:
#   * pdf_inventory — [{number, title, rows:[{cells}], raw_text}]
#   * pdf_checklist — sorted list of detected step numbers
# It also rewrites pre_sections down to the preamble (everything before Step 1)
# so pre_section_rule_extractor doesn't re-extract the procedure as loose rules.

# Page chrome / boilerplate lines that must never become step rows.
_RE_PAGE_MARKER = re.compile(r"^\s*-{1,3}\s*\d+\s+of\s+\d+\s*-{1,3}\s*$", re.I)
_RE_STEP_TABLE_HDR = re.compile(r"^\s*step\s*/?\s*action(\s+table)?\s*$", re.I)
_CHROME_EXACT = {
    "•", "↑ back to main m", "↑ back to main menu", "main menu",
    "step action", "step / action", "step/action",
}


def _pdf_is_chrome(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return True
    if s.lower() in _CHROME_EXACT:
        return True
    if _RE_PAGE_MARKER.match(s):
        return True
    if _RE_STEP_TABLE_HDR.match(s):
        return True
    if s.startswith("↑ Back to Main"):
        return True
    return False


def _pdf_split_cells(line: str) -> list[str]:
    """Split one PDF line into cells on tabs (or runs of 2+ spaces)."""
    parts = re.split(r"\t+|\s{2,}", line.strip())
    return [p.strip() for p in parts if p.strip()]


# A line begins a NEW logical row when it opens with a branch/condition cue;
# otherwise a single-cell line is treated as a wrapped continuation of the
# previous row and merged. This rebuilds the column structure PDF text loses.
_RE_ROW_START = re.compile(
    r"^(yes\b|no\b|if\b|and\b|then\b|meets\b|does not\b|all other|"
    r"[A-Z]{2,}\b|[\u2022\u25e6\u25aa•◦▪]|[-–]\s)",
    re.I,
)


def _pdf_consolidate_rows(rows: list[dict]) -> list[dict]:
    """Merge wrapped continuation lines so each row is one coherent cell grid."""
    out: list[dict] = []
    for r in rows:
        cells = r.get("cells", [])
        # Multi-cell rows already carry their own column structure.
        if len(cells) != 1:
            out.append(r)
            continue
        text = cells[0]
        starts_row = bool(_RE_ROW_START.match(text)) or text.endswith((":",))
        prev = out[-1] if out else None
        prev_is_single = prev is not None and len(prev["cells"]) == 1
        prev_text = prev["cells"][0] if prev_is_single else ""
        prev_open = prev_is_single and not prev_text.rstrip().endswith((".", "?", ":"))
        if prev_is_single and not starts_row and (
            text[:1].islower() or prev_open
        ):
            prev["cells"][0] = f"{prev_text} {text}".strip()
        else:
            out.append({"cells": [text]})
    return out


# A step starts on a line beginning with a bare integer followed by a
# tab/space and some text, e.g. "1\tIs your claim/line denying for TF1 or TF0?"
_RE_PDF_STEP_LINE = re.compile(r"^\s*(\d{1,3})[\.\)]?[\t ]+(\S.*)$")

# Headings that mark the END of the Step/Action procedure. Once seen, the
# current step stops absorbing lines so the final step doesn't swallow the
# code/terminology/revision appendices. Common across OBH Facets SOPs.
_PDF_STOP_HEADINGS = {
    "code descriptions and terminology", "code descriptions", "terminology",
    "revision history", "business details", "valid potf attachments",
    "invalid potf attachments", "appendix", "references",
}


def _pdf_is_stop_heading(line: str) -> bool:
    return (line or "").strip().lower().rstrip(":") in _PDF_STOP_HEADINGS


# Terminal step actions live entirely in their title (e.g. "Press (F4) to save
# the claim.") and never own decision rows. Treating them as self-contained
# stops the final terminal step from swallowing post-procedure appendices.
_PDF_TERMINAL_TOKENS = ("(f3)", "(f4)", "process the claim", "save the claim")


def _pdf_is_terminal_title(title: str) -> bool:
    t = (title or "").lower()
    return any(tok in t for tok in _PDF_TERMINAL_TOKENS)


def pdf_step_inventory(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    pre = state.get("pre_sections") or []
    if not pre:
        return {}

    # Flatten every page into ordered lines (tabs preserved by the extractor).
    lines: list[str] = []
    for s in pre:
        for item in s.get("items", []):
            txt = item.get("text", "") if isinstance(item, dict) else str(item)
            lines.append(txt)

    entries: dict[int, dict] = {}
    order: list[int] = []
    expected = 1
    current: int | None = None
    first_step_idx: int | None = None

    for idx, raw_line in enumerate(lines):
        m = _RE_PDF_STEP_LINE.match(raw_line)
        if m and int(m.group(1)) == expected and len(m.group(2).strip()) > 3:
            num = expected
            current = num
            if first_step_idx is None:
                first_step_idx = idx
            title = m.group(2).strip()[:300]
            entries[num] = {
                "number": num,
                "title": title,
                "rows": [],
                "raw_text": "",
                "source_html": "",
            }
            order.append(num)
            expected += 1
            # Terminal steps own no rows; don't let the last one absorb the
            # trailing code/terminology appendices.
            current = None if _pdf_is_terminal_title(title) else num
            continue

        if current is None:
            continue  # still in the preamble
        if _pdf_is_stop_heading(raw_line):
            current = None  # procedure ended — stop absorbing appendices
            continue
        if _pdf_is_chrome(raw_line):
            continue
        cells = _pdf_split_cells(raw_line)
        if cells:
            entries[current]["rows"].append({"cells": cells})

    if not entries:
        return {}

    for e in entries.values():
        e["rows"] = _pdf_consolidate_rows(e["rows"])
        e["raw_text"] = "\n".join(
            " | ".join(r["cells"]) for r in e["rows"])[:4000]

    inventory = [entries[n] for n in sorted(entries)]
    checklist = sorted(entries)
    # NOTE: written to the PDF-private ``pdf_inventory`` key (NOT the HTML
    # ``step_inventory``) so the PDF agentic flow stays fully separate from the
    # HTML step/reconciler path. Consumed by a06b.pdf_steps_agent.
    out: dict = {"pdf_inventory": inventory, "pdf_checklist": checklist}

    # Keep only the preamble (before Step 1) as pre_sections so the procedure
    # isn't double-counted by pre_section_rule_extractor.
    if first_step_idx is not None:
        preamble_lines = [ln for ln in lines[:first_step_idx]
                           if not _pdf_is_chrome(ln)]
        if preamble_lines:
            out["pre_sections"] = [{
                "name": "Overview",
                "order": 0,
                "section_id": "",
                "items": [{"text": ln.strip(), "item_type": "RULE",
                           "codes": [], "sub_items": []}
                          for ln in preamble_lines],
                "annotations": [],
                "source_html": "",
            }]
        else:
            out["pre_sections"] = []

    logger.info("pdf_step_inventory: checklist=%s (%d steps)",
                checklist, len(checklist))
    return out
