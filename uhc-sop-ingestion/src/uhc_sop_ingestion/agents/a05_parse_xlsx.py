"""XLSX PARSE LAYER — 6 agents.

1. XLSXWorkbookTypeAgent   — CODE_TABLE | CALCULATOR_TOOL | RULE_TABLE | GENERIC
2. XLSXSheetParserAgent    — reads each sheet: headers + rows
3. XLSXHeaderDetectorAgent — finds the true header row (skips blanks)
4. XLSXCodeExtractorAgent  — maps rows to code_table_entries
5. XLSXCalculatorAgent     — extracts schema of CALCULATOR_TOOL workbooks
6. XLSXMetadataAgent       — workbook title + sheet names → metadata
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


def _wb(state):
    from openpyxl import load_workbook
    b64 = state.get("raw_bytes_b64","")
    if not b64: return None
    return load_workbook(io.BytesIO(base64.b64decode(b64)), read_only=True, data_only=True)


def _infer_code_system(headers: list[str], sheet_name: str) -> str:
    h = " ".join(headers + [sheet_name]).lower()
    if "eob" in h: return "EOB"
    if "ex code" in h or "ex_code" in h: return "EX"
    if "pos" in h or "place of service" in h: return "POS"
    if "revenue" in h or "rev code" in h: return "REVENUE"
    if "modifier" in h: return "MODIFIER"
    if "cpt" in h or "hcpcs" in h: return "CPT"
    if "denial" in h: return "DENIAL"
    if "term" in h or "glossary" in h or "abbreviat" in h: return "TERM"
    return "UNKNOWN"

def _find_col(headers, candidates):
    for h in headers:
        if any(c.lower() in h.lower() for c in candidates): return h
    return None

def _header_row_idx(ws) -> int:
    for i, row in enumerate(ws.iter_rows(max_row=5, values_only=True), 1):
        non_empty = [c for c in row if c is not None]
        if not non_empty: continue
        if sum(1 for c in non_empty if isinstance(c, str)) / len(non_empty) >= 0.6:
            return i
    return 1


# ── 1. XLSXWorkbookTypeAgent ──────────────────────────────────────────────────

def xlsx_workbook_type(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    wb = _wb(state)
    if not wb: return {}
    all_headers, all_names = [], []
    for name in wb.sheetnames:
        ws = wb[name]
        all_names.append(name)
        idx = _header_row_idx(ws)
        for i, row in enumerate(ws.iter_rows(max_row=5, values_only=True), 1):
            if i == idx:
                all_headers.extend([str(c) for c in row if c])
                break

    combined = " ".join(all_headers + all_names).lower()
    if "service date" in combined or "calculator" in " ".join(all_names).lower():
        wb_type = "CALCULATOR_TOOL"
    elif ("code" in combined or "eob" in combined or "cpt" in combined) and (
        "description" in combined or "deny" in combined or "allow" in combined):
        wb_type = "CODE_TABLE"
    elif any(w in combined for w in ["if","then","allow","deny"]):
        wb_type = "RULE_TABLE"
    else:
        wb_type = "GENERIC"

    return {"xlsx_workbook_type": wb_type}


# ── 2. XLSXSheetParserAgent ───────────────────────────────────────────────────

def xlsx_sheet_parser(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Parses all sheets and stores them in reference_tables."""
    wb = _wb(state)
    if not wb: return {}
    ref_tables = []
    raw_text_parts = []

    for name in wb.sheetnames:
        ws = wb[name]
        idx = _header_row_idx(ws)
        headers: list[str] = []
        rows: list[dict] = []

        for i, row in enumerate(ws.iter_rows(values_only=True), 1):
            if i == idx:
                headers = [str(c).strip() if c is not None else f"col_{j}"
                           for j, c in enumerate(row)]
            elif i > idx:
                if not any(c is not None for c in row): continue
                row_dict = {headers[j]: c for j, c in enumerate(row) if j < len(headers)}
                rows.append(row_dict)
                raw_text_parts.append(" | ".join(str(v) for v in row_dict.values() if v))

        if not headers: continue
        tbl = {"name": name, "table_type": "GENERIC",
               "rows": [{"label": str(list(r.values())[0]) if r else "",
                         "content": " | ".join(str(v) for v in list(r.values())[1:] if v),
                         "row_type": "ITEM"}
                        for r in rows if r]}
        ref_tables.append(tbl)

    return {"reference_tables": ref_tables, "raw_text": "\n".join(raw_text_parts)}


# ── 3. XLSXHeaderDetectorAgent ────────────────────────────────────────────────

def xlsx_header_detector(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Validates header detection — logs warning if first sheet has no clear header."""
    wb = _wb(state)
    if not wb or not wb.sheetnames: return {}
    ws = wb[wb.sheetnames[0]]
    idx = _header_row_idx(ws)
    if idx > 2:
        return {"parse_warnings": [f"xlsx: header row detected at row {idx} (skipped {idx-1} blank rows)"]}
    return {}


# ── 4. XLSXCodeExtractorAgent ─────────────────────────────────────────────────

def xlsx_code_extractor(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    if state.get("xlsx_workbook_type") not in ("CODE_TABLE", "GENERIC"): return {}
    wb = _wb(state)
    if not wb: return {}
    entries = list(state.get("code_table_entries") or [])

    for name in wb.sheetnames:
        ws = wb[name]
        idx = _header_row_idx(ws)
        headers: list[str] = []
        for i, row in enumerate(ws.iter_rows(max_row=idx, values_only=True), 1):
            if i == idx:
                headers = [str(c).strip() if c else f"col_{j}" for j, c in enumerate(row)]
                break

        code_sys = _infer_code_system(headers, name)
        col_code  = _find_col(headers, ["Code","CPT","HCPCS","EOB Code","EX Code","Procedure"])
        col_desc  = _find_col(headers, ["Description","Desc","Definition","Meaning","Name"])
        col_allow = _find_col(headers, ["Allow","Prevailing Code","Allowed Code"])
        col_deny  = _find_col(headers, ["Deny","Deny Code","Denied Code"])
        if not col_code: continue

        for i, row in enumerate(ws.iter_rows(values_only=True), 1):
            if i <= idx: continue
            row_dict = {headers[j]: v for j, v in enumerate(row) if j < len(headers)}
            code_val = str(row_dict.get(col_code, "") or "").strip()
            if not code_val: continue
            entries.append({
                "code": code_val,
                "code_system": code_sys,
                "description": str(row_dict.get(col_desc,"") or "").strip(),
                "allow_code": str(row_dict.get(col_allow,"") or "") or None if col_allow else None,
                "deny_code":  str(row_dict.get(col_deny, "") or "") or None if col_deny else None,
                "source_sheet": name,
                "extra_fields": {k: v for k, v in row_dict.items()
                                 if k not in {col_code,col_desc,col_allow,col_deny} and v is not None},
            })
    return {"code_table_entries": entries}


# ── 5. XLSXCalculatorAgent ────────────────────────────────────────────────────

def xlsx_calculator(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    if state.get("xlsx_workbook_type") != "CALCULATOR_TOOL": return {}
    wb = _wb(state)
    if not wb: return {}
    schema_items = []
    for name in wb.sheetnames:
        ws = wb[name]
        for i, row in enumerate(ws.iter_rows(max_row=20, values_only=True), 1):
            labels = [str(c).strip() for c in row if c and isinstance(c, str)]
            if labels:
                schema_items.append({"text": f"[Sheet:{name}] {' | '.join(labels)}",
                                     "item_type": "TOOL_SCHEMA", "codes": [], "sub_items": []})

    section = {"name": f"Calculator Schema: {state.get('metadata',{}).get('title','XLSX')}",
               "order": 0, "section_id": "", "items": schema_items, "annotations": []}
    pre = list(state.get("pre_sections") or [])
    pre.insert(0, section)
    return {"pre_sections": pre}


# ── 6. XLSXMetadataAgent ──────────────────────────────────────────────────────

def xlsx_metadata(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    wb = _wb(state)
    if not wb: return {}
    url = state.get("current_url", "")
    title = wb.properties.title or url.rsplit("/",1)[-1].rsplit(".",1)[0]
    meta = dict(state.get("metadata") or {})
    meta["title"] = title
    meta["sheets"] = wb.sheetnames
    return {"metadata": meta}
