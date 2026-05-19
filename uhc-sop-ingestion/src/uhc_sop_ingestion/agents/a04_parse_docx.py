"""DOCX PARSE LAYER — 6 agents.

1. DOCXMetadataAgent     — built-in properties → title, dates
2. DOCXHeadingAgent      — headings → pre_section nodes
3. DOCXParagraphAgent    — bullet paragraphs → pre_section items
4. DOCXTableAgent        — tables → decision rows or code tables
5. DOCXCodeTableAgent    — CODE_LOOKUP / GLOSSARY tables → code_table_entries
6. DOCXHyperlinkAgent    — OOXML rels → DiscoveredLink list
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

_EXT_LT = {".xlsx":"XLSX",".xls":"XLSX",".docx":"DOCX",".doc":"DOCX",".pdf":"PDF"}


def _doc(state):
    from docx import Document
    b64 = state.get("raw_bytes_b64","")
    if not b64: return None
    return Document(io.BytesIO(base64.b64decode(b64)))


def _classify_table(headers: list[str]) -> str:
    h = " ".join(headers).lower()
    if ("code" in h or "eob" in h) and ("desc" in h or "definition" in h or "meaning" in h):
        return "CODE_LOOKUP"
    if "term" in h or "abbreviat" in h or "acronym" in h:
        return "GLOSSARY"
    if any(w in h for w in ["if","then"]) and len(headers) == 2: return "BINARY_DECISION"
    if any(w in h for w in ["if","then"]) and len(headers) >= 3: return "COMPOUND_DECISION"
    return "GENERIC"


# ── 1. DOCXMetadataAgent ─────────────────────────────────────────────────────

def docx_metadata(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    doc = _doc(state)
    if not doc: return {}
    p = doc.core_properties
    title = p.title or ""
    if not title:
        for para in doc.paragraphs[:5]:
            if para.style and "heading" in para.style.name.lower() and para.text.strip():
                title = para.text.strip(); break
            elif para.text.strip():
                title = para.text.strip(); break
    meta = dict(state.get("metadata") or {})
    meta.update({
        "title": title,
        "effective_date": str(p.created.date()) if p.created else "",
        "revision_date":  str(p.modified.date()) if p.modified else "",
    })
    return {"metadata": meta}


# ── 2. DOCXHeadingAgent ───────────────────────────────────────────────────────

def docx_headings(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    doc = _doc(state)
    if not doc: return {}
    sections = []
    order = 0
    for para in doc.paragraphs:
        style = para.style.name.lower() if para.style else ""
        if "heading" in style and para.text.strip():
            sections.append({"name": para.text.strip(), "order": order,
                             "section_id": "", "items": [], "annotations": []})
            order += 1
    return {"pre_sections": sections}


# ── 3. DOCXParagraphAgent ─────────────────────────────────────────────────────

def docx_paragraphs(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    doc = _doc(state)
    if not doc: return {}
    sections = [dict(s) for s in (state.get("pre_sections") or [])]
    current_section = sections[-1] if sections else None

    for para in doc.paragraphs:
        style = para.style.name.lower() if para.style else ""
        text  = para.text.strip()
        if not text: continue
        if "heading" in style: continue  # handled by docx_headings

        item_type = "NOTE" if "note" in text.lower() else "RULE"
        is_bullet = "list" in style or text.startswith(("•","–","-","*"))

        if current_section is not None:
            current_section["items"].append({"text": text, "item_type": item_type,
                                             "codes": [], "sub_items": []})
    return {"pre_sections": sections}


# ── 4. DOCXTableAgent ─────────────────────────────────────────────────────────

def docx_tables(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    doc = _doc(state)
    if not doc: return {}
    steps = list(state.get("steps") or [])
    ref_tables = list(state.get("reference_tables") or [])
    step_counter = (max(s["number"] for s in steps) + 1) if steps else 1

    for table in doc.tables:
        if not table.rows: continue
        headers = [cell.text.strip() for cell in table.rows[0].cells]
        tt = _classify_table(headers)
        rows = []
        for row in table.rows[1:]:
            cells = [c.text.strip() for c in row.cells]
            if any(cells): rows.append(cells)

        if tt in ("BINARY_DECISION","COMPOUND_DECISION"):
            step = {"number": step_counter, "question": headers[0] if headers else "",
                    "decision_rows": [], "annotations": [], "branch_yes": "",
                    "branch_no": "", "raw_text": " | ".join(headers)}
            for row in rows:
                dr = {"condition_if": row[0] if len(row)>0 else "",
                      "condition_and": row[1] if len(row)>1 and tt=="COMPOUND_DECISION" else "",
                      "action": row[-1], "decision": "CONDITIONAL", "codes": [],
                      "skip_to_step": None, "routing_label": "", "referenced_sops": [], "annotations": []}
                step["decision_rows"].append(dr)
            steps.append(step)
            step_counter += 1
        elif tt in ("CODE_LOOKUP","GLOSSARY"):
            tbl = {"name": " | ".join(headers), "table_type": tt, "rows": []}
            for row in rows:
                if len(row) >= 2:
                    tbl["rows"].append({"label": row[0], "content": row[1], "row_type": "CODE"})
            ref_tables.append(tbl)

    return {"steps": steps, "reference_tables": ref_tables}


# ── 5. DOCXCodeTableAgent ─────────────────────────────────────────────────────

def docx_code_tables(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    doc = _doc(state)
    if not doc: return {}
    entries = list(state.get("code_table_entries") or [])

    for table in doc.tables:
        if not table.rows: continue
        headers = [c.text.strip() for c in table.rows[0].cells]
        tt = _classify_table(headers)
        if tt not in ("CODE_LOOKUP","GLOSSARY"): continue

        code_sys = _infer_code_system(headers)
        col_code = _find_col(headers, ["Code","EOB","EX Code","CPT","Term","Abbreviation"])
        col_desc = _find_col(headers, ["Description","Desc","Definition","Meaning","Expansion"])
        if not col_code: continue

        for row in table.rows[1:]:
            cells = [c.text.strip() for c in row.cells]
            if not cells: continue
            ci = headers.index(col_code) if col_code in headers else 0
            di = headers.index(col_desc) if col_desc and col_desc in headers else 1
            code_val = cells[ci] if ci < len(cells) else ""
            desc_val = cells[di] if di < len(cells) else ""
            if code_val:
                entries.append({"code": code_val, "code_system": code_sys,
                                "description": desc_val, "allow_code": None,
                                "deny_code": None, "source_sheet": "docx"})
    return {"code_table_entries": entries}


def _infer_code_system(headers: list[str]) -> str:
    h = " ".join(headers).lower()
    if "eob" in h: return "EOB"
    if "ex code" in h: return "EX"
    if "pos" in h or "place of service" in h: return "POS"
    if "revenue" in h: return "REVENUE"
    if "modifier" in h: return "MODIFIER"
    if "cpt" in h or "hcpcs" in h: return "CPT"
    if "term" in h or "abbreviat" in h: return "TERM"
    return "UNKNOWN"

def _find_col(headers: list[str], candidates: list[str]):
    for h in headers:
        if any(c.lower() in h.lower() for c in candidates): return h
    return None


# ── 6. DOCXHyperlinkAgent ─────────────────────────────────────────────────────

def docx_hyperlinks(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    doc = _doc(state)
    if not doc: return {}
    links = list(state.get("links") or [])
    try:
        for rel in doc.part.rels.values():
            if "hyperlink" not in rel.reltype.lower(): continue
            href = rel.target_ref
            ext  = href.rsplit(".",1)[-1].lower() if "." in href else ""
            lt   = _EXT_LT.get(f".{ext}", "HTML_SOP")
            links.append({"href": href, "text": href, "link_type": lt,
                          "resolved_url": href if href.startswith("http") else None,
                          "is_resolved": href.startswith("http")})
    except Exception as e:
        logger.debug("docx_hyperlinks: %s", e)
    return {"links": links}
