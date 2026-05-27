"""HTML PARSE LAYER — 12 agents.

DESIGN PRINCIPLE: Zero hardcoded CSS class names, IDs, or tag structures.
Every detection is driven by content heuristics and structural patterns so
any SOP HTML template works without configuration.

Detection strategies
────────────────────
Metadata       : <title>, <h1>, prominent headings, date regexes anywhere
Biz table      : any table whose header row mentions Platform/Audience/LOB
Pre-sections   : any label+content row pattern (first cell short+bold-ish,
                 second cell longer) in non-step tables
Steps          : any table column whose consecutive cells are pure integers
                 (1, 2, 3 …) — the sibling cell is the action content
Decision tables: inner tables whose first <th> text matches "if|condition"
Compound tables: 3-col inner tables whose headers match if/and/then
Group rules    : 2-col tables with time-period keywords (days/months/years)
                 and no pure-integer first column
Annotations    : elements whose inline style has highlight colours, or whose
                 text starts with Note:/Alert:/Warning:/Exception:, or whose
                 tag is <blockquote>/<aside>
Reference tbls : table rows with "valid"/"invalid"+"attachment" in first cell,
                 or td.lbl rows whose text contains "code"
Sub-procedures : named numbered step-sequences separated from the main flow
                 (identified when multiple sequential integer blocks exist)
Links          : all <a href> — unchanged
"""
from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)


# ── Shared regex patterns (content-driven, not structure-driven) ──────────────

_RE_DATE       = re.compile(r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})")
_RE_EFF_DATE   = re.compile(r"(?:original\s+)?effective\s+date[:\s]*([\d/\-]+)", re.I)
_RE_REV_DATE   = re.compile(r"revision\s+date[:\s]*([\d/\-]+)", re.I)
_RE_CODE       = re.compile(
    r"\b([EFW]\d{2,3})\b"
    r"|EX\s*[Cc]ode\s+([A-Za-z0-9]{2,5})"
    r"|\b(OCA|o01|020|003|346|CDD|F3|F4|F5)\b"
)
_RE_SKIP       = re.compile(r"[Ss]kip\s+to\s+[Ss]tep\s+(\d+)")
_RE_DAYS       = re.compile(r"(\d+)\s+(days?|months?|years?)", re.I)
_RE_DOC_INDS   = re.compile(
    r"\b(P&P|Policy|Procedure|Calculator|Codes?\s+List|Bulletin|Process|"
    r"Validation|Instruction|Glossary|Attachment|P&P)\b", re.I
)
_NOTE_PREFIX   = re.compile(r"^\s*(note|alert|warning|exception|tip|important)[:\s*]", re.I)
_ANN_COLOURS   = {  # highlight background colour fragments → annotation type
    "fff176": "HIGHLIGHT", "ffff00": "HIGHLIGHT",
    "ffe0b2": "HIGHLIGHT", "fce4d6": "HIGHLIGHT",
    "fff9e6": "NOTE",      "eaf4fb": "NOTE",
    "e8f4e8": "TIP",       "fdecea": "ALERT",
    "fff3cd": "ALERT",
}
_BIZ_HEADERS   = {"platform","audience","lob","line of business",
                   "product","state","div","contact"}
_IF_HEADERS    = {"if", "if…", "if...", "condition", "situation", "when"}
_AND_HEADERS   = {"and", "and…", "and...", "also", "plus"}
_THEN_HEADERS  = {"then", "then…", "then...", "action", "result", "outcome"}


# ── Utility functions ─────────────────────────────────────────────────────────

def _t(tag) -> str:
    """Get text from a BS4 tag, normalised."""
    return tag.get_text(" ", strip=True) if tag else ""


def _codes(text: str) -> list[str]:
    seen, out = set(), []
    for m in _RE_CODE.finditer(text):
        c = next(g for g in m.groups() if g)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _skip_to(text: str):
    m = _RE_SKIP.search(text)
    return int(m.group(1)) if m else None


def _looks_like_doc_ref(text: str) -> bool:
    return len(text.split()) >= 3 and bool(_RE_DOC_INDS.search(text))


def _guess_decision(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["deny", "denial", "zero out", "reduce to 0"]):
        return "DENY"
    if "bypass" in t:
        return "BYPASS"
    if "allow" in t:
        return "ALLOW"
    if "pend" in t or "f5" in t:
        return "PEND"
    if "waive" in t:
        return "WAIVE"
    if any(w in t for w in ["refer to", "follow the", "proceed to", "skip to"]):
        return "REFER"
    if any(w in t for w in ["does not apply", "this p&p"]):
        return "STOP"
    if any(w in t for w in ["(f3)", "(f4)", "process the claim", "save the claim"]):
        return "SYSTEM"
    return "CONDITIONAL"


def _soup(state) -> "BeautifulSoup | None":
    """Pass raw bytes to BeautifulSoup so it detects encoding from <meta charset>.
    Never pre-decode with a guessed encoding — that corrupts multi-byte UTF-8
    sequences (em-dash, bullet, etc.) when the HTTP layer guesses wrong."""
    from bs4 import BeautifulSoup
    b64 = state.get("raw_bytes_b64", "")
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        return BeautifulSoup(raw, "lxml")   # BS4 reads <meta charset> itself
    except Exception as exc:
        logger.warning("html_decode: %s", exc)
        return None


def _inline_bg_colour(el) -> str | None:
    """Return lowercase hex from an element's inline background colour, or None."""
    style = el.get("style", "")
    m = re.search(r"background(?:-color)?\s*:\s*#?([0-9a-fA-F]{3,6})", style)
    return m.group(1).lower() if m else None


def _ann_type_from_colour(hex_colour: str | None) -> str | None:
    if not hex_colour:
        return None
    hex6 = hex_colour.zfill(6)
    for frag, atype in _ANN_COLOURS.items():
        if frag in hex6:
            return atype
    return None


def _is_pure_int(text: str) -> bool:
    return text.strip().isdigit()


# ── Step-table detection ──────────────────────────────────────────────────────

def _find_step_tables(soup) -> list:
    """Return (table, col_index, step_numbers) for every table that looks like a
    step/action table.  Detection: a column whose values are consecutive integers
    starting from 1 with >= 2 rows.

    Handles both flat <table><tr>... and nested <table><thead>/<tbody><tr>...
    """
    result = []
    for table in soup.find_all("table"):
        # Get all <tr> rows regardless of whether they're in <tbody>/<thead>
        # Use recursive=True so we catch rows inside <tbody>, <thead>, <tfoot>
        all_trs = table.find_all("tr")
        if len(all_trs) < 3:  # need at least header + 2 steps
            continue
        # Exclude <tr> rows that belong to nested inner tables
        direct_trs = [tr for tr in all_trs
                      if tr.find_parent("table") == table]
        if len(direct_trs) < 3:
            continue
        data_rows = [r for r in direct_trs if r.find("td")]
        if len(data_rows) < 2:
            continue
        # Try column 0 and column 1
        for col in (0, 1):
            nums = []
            for tr in data_rows:
                cells = tr.find_all(["td", "th"], recursive=False)
                if col >= len(cells):
                    break
                txt = cells[col].get_text(strip=True)
                if _is_pure_int(txt):
                    nums.append(int(txt))
                else:
                    break
            if len(nums) >= 2 and nums == list(range(nums[0], nums[0] + len(nums))):
                result.append((table, col, nums))
                break  # found the col for this table
    return result


# ── Section-header detection ──────────────────────────────────────────────────

def _is_section_header_row(tr) -> bool:
    """True if the row is a visual section header (spans all cols, dark bg)."""
    cells = tr.find_all(["td", "th"], recursive=False)
    if not cells:
        return False
    # Spanning cell
    c = cells[0]
    colspan = int(c.get("colspan", 1))
    if colspan < 2:
        return False
    # Text is short-ish (not body content)
    txt = _t(c)
    return 2 < len(txt) < 120


def _is_label_row(tr) -> bool:
    """True if the row has a short first cell (label) and longer second cell."""
    cells = tr.find_all("td", recursive=False)
    if len(cells) < 2:
        return False
    label_len = len(_t(cells[0]))
    content_len = len(_t(cells[1]))
    if label_len == 0 or content_len == 0:
        return False
    # Label is significantly shorter than content, or cell has header styling
    return label_len <= 80 and content_len > label_len


# ── Annotation extraction from a single tag ───────────────────────────────────

def _extract_anns(tag) -> list[dict]:
    """Extract all annotation boxes/notes inside a tag."""
    anns = []
    seen_texts: set[str] = set()

    def _add(atype, el):
        txt = _t(el)
        if txt and txt not in seen_texts:
            seen_texts.add(txt)
            anns.append({"annotation_type": atype, "text": txt})

    # Block-level tags: blockquote, aside, div, p with highlight style
    for tag_name in ("blockquote", "aside"):
        for el in tag.find_all(tag_name):
            _add("NOTE", el)

    # Any element with highlight/note background colour in inline style
    for el in tag.find_all(True):
        bg = _inline_bg_colour(el)
        atype = _ann_type_from_colour(bg)
        if atype:
            _add(atype, el)

    # Elements whose text starts with Note:/Alert:/Warning: etc.
    for el in tag.find_all(["p", "div", "span", "strong", "em", "li"]):
        txt = _t(el)
        m = _NOTE_PREFIX.match(txt)
        if m:
            kw = m.group(1).upper()
            atype = {"NOTE": "NOTE", "ALERT": "ALERT", "WARNING": "ALERT",
                     "EXCEPTION": "EXCEPTION", "TIP": "TIP",
                     "IMPORTANT": "NOTE"}.get(kw, "NOTE")
            _add(atype, el)

    return anns


# ── Items extraction from a content cell ─────────────────────────────────────

def _items_from_td(td) -> list[dict]:
    items = []
    seen: set[str] = set()

    # Bullet list items (top-level only)
    for li in td.find_all("li"):
        if li.find_parent("li"):
            continue
        txt = li.get_text(" ", strip=True)
        if txt and txt not in seen:
            seen.add(txt)
            items.append({
                "text": txt, "item_type": "RULE",
                "codes": _codes(txt),
                "sub_items": [{"text": sub.get_text(" ", strip=True)}
                               for sub in li.find_all("li")],
            })

    # Plain paragraph text (not already captured)
    for p in td.find_all(["p", "div"]):
        txt = p.get_text(" ", strip=True)
        if txt and txt not in seen and len(txt) > 10:
            seen.add(txt)
            items.append({
                "text": txt, "item_type": "INFO",
                "codes": _codes(txt), "sub_items": [],
            })

    return items


# ─────────────────────────────────────────────────────────────────────────────
# 1.  HTMLDecodeAgent
# ─────────────────────────────────────────────────────────────────────────────

def html_decode(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    b64 = state.get("raw_bytes_b64", "")
    if not b64:
        return {"parse_warnings": ["html_decode: no raw bytes"]}
    try:
        base64.b64decode(b64).decode(state.get("encoding", "utf-8") or "utf-8",
                                      errors="replace")
        return {"parse_warnings": []}
    except Exception as e:
        return {"parse_warnings": [f"html_decode: {e}"]}


# ─────────────────────────────────────────────────────────────────────────────
# 2.  HTMLMetadataAgent
# ─────────────────────────────────────────────────────────────────────────────

def html_metadata(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    soup = _soup(state)
    if not soup:
        return {}

    meta = {
        "title": "", "effective_date": "", "revision_date": "",
        "platform": "", "lob": [], "audience": [], "state_div": "", "product": "",
    }

    # ── Title: try candidate sources in order of confidence ──────────────────
    title_candidates = []
    # <title> tag
    t = soup.find("title")
    if t:
        title_candidates.append(_t(t))
    # <h1>
    h1 = soup.find("h1")
    if h1:
        title_candidates.append(_t(h1))
    # First large heading-like table cell (full-width, short text)
    for td in soup.find_all(["td", "th"]):
        txt = _t(td)
        if 8 < len(txt) < 120 and int(td.get("colspan", 1)) >= 2:
            title_candidates.append(txt)
            break

    for cand in title_candidates:
        cleaned = re.sub(r"\s+", " ", cand).strip()
        if cleaned:
            meta["title"] = cleaned
            break

    # ── Dates: scan entire text of the document ───────────────────────────────
    full_text = soup.get_text(" ")
    m = _RE_EFF_DATE.search(full_text)
    if m:
        meta["effective_date"] = m.group(1)
    m = _RE_REV_DATE.search(full_text)
    if m:
        meta["revision_date"] = m.group(1)

    return {"metadata": meta}


# ─────────────────────────────────────────────────────────────────────────────
# 3.  HTMLBizTableAgent
# ─────────────────────────────────────────────────────────────────────────────

def html_biz_table(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    soup = _soup(state)
    if not soup:
        return {}
    meta = dict(state.get("metadata") or {})

    for table in soup.find_all("table"):
        # Find the header row
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [_t(c).lower() for c in header_row.find_all(["th", "td"])]
        matched = sum(1 for h in headers if any(b in h for b in _BIZ_HEADERS))
        if matched < 2:
            continue

        # Map header index → field name
        hmap: dict[int, str] = {}
        for i, h in enumerate(headers):
            if "platform" in h:       hmap[i] = "platform"
            elif "audience" in h:     hmap[i] = "audience"
            elif "lob" in h or "line of business" in h: hmap[i] = "lob"
            elif "product" in h:      hmap[i] = "product"
            elif "state" in h or "div" in h: hmap[i] = "state_div"

        data_rows = table.find_all("tr")[1:]
        for tr in data_rows:
            cells = tr.find_all(["td", "th"])
            for idx, field in hmap.items():
                if idx >= len(cells):
                    continue
                val = _t(cells[idx])
                if not val:
                    continue
                if field in ("lob", "audience"):
                    items = [v.strip() for v in re.split(r"[\n,]", val) if v.strip()]
                    meta[field] = items if items else [val]
                else:
                    meta[field] = val
        break  # first matching table is the biz table

    return {"metadata": meta}


# ─────────────────────────────────────────────────────────────────────────────
# 4.  HTMLPreSectionAgent
# ─────────────────────────────────────────────────────────────────────────────

def html_pre_sections(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Detect any label→content row pattern in non-step, non-biz tables."""
    soup = _soup(state)
    if not soup:
        return {}

    # Tables that are step tables — skip them
    step_table_ids = {id(t) for t, _, _ in _find_step_tables(soup)}
    # Tables that are biz tables — skip them
    biz_table_ids: set[int] = set()
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [_t(c).lower() for c in header_row.find_all(["th", "td"])]
        if sum(1 for h in headers if any(b in h for b in _BIZ_HEADERS)) >= 2:
            biz_table_ids.add(id(table))

    sections = []
    order = 0
    seen_names: set[str] = set()

    # Walk every table row
    for table in soup.find_all("table"):
        if id(table) in step_table_ids or id(table) in biz_table_ids:
            continue
        for tr in table.find_all("tr"):
            if _is_section_header_row(tr):
                continue
            if not _is_label_row(tr):
                continue
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 2:
                continue
            name = _t(cells[0]).strip()
            if not name or name in seen_names:
                continue
            # Skip pure header-sounding entries that are actually column headers
            if name.lower() in {"step", "action", "if", "then", "and",
                                 "category", "details", "group", "guidelines",
                                 "if…", "and…", "then…"}:
                continue
            seen_names.add(name)
            sid = cells[0].get("id", "")
            # outerHTML of the content cell — used by html_dom.block_id_for_html
            # to cross-link this pre-section back to the matching :HtmlBlock
            # in Neo4j via (:GraphNode)-[:DERIVED_FROM]->(:HtmlBlock).
            source_html = str(cells[1])
            sections.append({
                "name": name,
                "order": order,
                "section_id": sid,
                "items": _items_from_td(cells[1]),
                "annotations": _extract_anns(cells[1]),
                "source_html": source_html,
            })
            order += 1

    # Also handle div/section-based layouts (no table):
    # .section-block > .section-title + content
    for block in soup.find_all(True):
        # Find elements whose first child is a short heading-like element
        children = [c for c in block.children if hasattr(c, "name") and c.name]
        if len(children) < 2:
            continue
        heading = children[0]
        content = children[1]
        htxt = _t(heading).strip()
        ctxt = _t(content).strip()
        if not htxt or not ctxt:
            continue
        if len(htxt) > 80 or len(ctxt) < len(htxt):
            continue
        if htxt in seen_names:
            continue
        # Only process if it looks like a dedicated labeled section (not a table row)
        if heading.name not in ("div", "span", "p", "h2", "h3", "h4", "dt"):
            continue
        seen_names.add(htxt)
        sections.append({
            "name": htxt,
            "order": order,
            "section_id": block.get("id", ""),
            "items": _items_from_td(content),
            "annotations": _extract_anns(content),
            "source_html": str(block),
        })
        order += 1

    return {"pre_sections": sections}


# ─────────────────────────────────────────────────────────────────────────────
# 5.  HTMLStepAgent
# ─────────────────────────────────────────────────────────────────────────────

def html_steps(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Detect step tables purely by consecutive-integer column pattern.

    When multiple integer sequences exist in the document, the one that starts
    at 1 and has the most rows is the main step table. Others are sub-procedures
    and are excluded here (handled by HTMLSubProcedureAgent).
    """
    soup = _soup(state)
    if not soup:
        return {}

    candidates = _find_step_tables(soup)
    if not candidates:
        return {"steps": []}

    # Pick the primary step sequence: starts at 1, most steps
    def _score(entry):
        _, _, nums = entry
        starts_at_1 = 1 if nums[0] == 1 else 0
        return (starts_at_1, len(nums))

    candidates.sort(key=_score, reverse=True)
    primary_table, num_col, nums = candidates[0]
    # Record which tables are sub-procedure tables (not primary)
    sub_table_ids = {id(t) for t, _, _ in candidates[1:]}

    steps = []
    seen: set[int] = set()
    # Only direct rows of the primary table (not rows inside nested inner tables)
    data_rows = [r for r in primary_table.find_all("tr")
                 if r.find("td") and r.find_parent("table") == primary_table]

    for tr in data_rows:
        cells = tr.find_all(["td", "th"], recursive=False)
        if num_col >= len(cells):
            continue
        num_txt = cells[num_col].get_text(strip=True)
        if not _is_pure_int(num_txt):
            continue
        num = int(num_txt)
        if num in seen:
            continue
        seen.add(num)

        # Content cell is whichever is NOT the number cell
        content_col = 1 if num_col == 0 else 0
        if content_col >= len(cells):
            continue
        action_td = cells[content_col]

        step = _parse_step_cell(num, action_td)
        # outerHTML of the parent <tr> — used by html_dom.block_id_for_html
        # to back-link this step's :Step / :Decision GraphNodes to the
        # corresponding :HtmlBlock via :DERIVED_FROM in Neo4j.
        step["source_html"] = str(tr)

        # Embed any inner group table found in this step
        inner_group = _detect_group_table(action_td)
        if inner_group:
            step["group_rows"] = inner_group

        steps.append(step)

    steps.sort(key=lambda s: s["number"])
    return {"steps": steps}


def _parse_step_cell(num: int, td) -> dict:
    full_text = _t(td)
    step = {
        "number": num,
        "question": "",
        "intro_text": "",
        "decision_rows": [],
        "annotations": _extract_anns(td),
        "branch_yes": "",
        "branch_no": "",
        "skip_to_step_yes": None,
        "skip_to_step_no": None,
        "referenced_sops": [],
        "is_terminal": any(tok in full_text for tok in ["(F3)", "(F4)", "process the claim", "save the claim"]),
        "raw_text": full_text,
    }

    # Question: first prominent text not inside a list or table
    for el in td.children:
        if not hasattr(el, "name"):
            continue
        if el.name in ("p", "strong", "b", "em"):
            txt = _t(el).strip()
            if txt and not txt.startswith(("Note", "Alert", "Warning", "Exception")):
                step["question"] = txt
                break
        elif el.name not in ("table", "ul", "ol", "div"):
            txt = el.get_text(strip=True)
            if txt and len(txt) > 10:
                step["question"] = txt
                break

    # Yes/No branches from bullet list items — also build them as decision_rows
    # so the downstream write layer treats them identically to If/Then table rows.
    _YES_PAT = re.compile(r"^yes\b", re.I)
    _NO_PAT  = re.compile(r"^no\b",  re.I)
    _ITEM_PAT = re.compile(r"^(yes|no|all items match|any item[s/]* (?:do )?not match|"
                            r"11[,\s]|02[,\s]|correct|incorrect|true|false)[:\s–\-]",
                            re.I)
    for li in td.find_all("li", recursive=True):
        # Skip nested lis that are bullet details, not decisions
        if li.find_parent("li") and li.find_parent("li").find_parent("li"):
            continue
        txt = li.get_text(" ", strip=True)
        low = txt.lower()
        if _YES_PAT.match(low):
            step["branch_yes"] = txt
            step["skip_to_step_yes"] = _skip_to(txt)
            # Also add as a structured decision row
            step["decision_rows"].append({
                "condition_if": "Yes",
                "condition_and": "",
                "action": re.sub(r"^yes\s*[–\-:]\s*", "", txt, flags=re.I).strip(),
                "decision": _guess_decision(txt),
                "codes": _codes(txt),
                "skip_to_step": _skip_to(txt),
                "routing_label": "",
                "referenced_sops": [],
                "annotations": [],
                "source_html": str(li),
            })
        elif _NO_PAT.match(low):
            step["branch_no"] = txt
            step["skip_to_step_no"] = _skip_to(txt)
            step["decision_rows"].append({
                "condition_if": "No",
                "condition_and": "",
                "action": re.sub(r"^no\s*[–\-:]\s*", "", txt, flags=re.I).strip(),
                "decision": _guess_decision(txt),
                "codes": _codes(txt),
                "skip_to_step": _skip_to(txt),
                "routing_label": "",
                "referenced_sops": [],
                "annotations": [],
                "source_html": str(li),
            })
        elif _ITEM_PAT.match(txt):
            # Generic bullet that represents a condition branch (e.g. "All items match: Skip to Step 4")
            step["decision_rows"].append({
                "condition_if": txt.split(":")[0].strip() if ":" in txt else txt[:60],
                "condition_and": "",
                "action": txt.split(":", 1)[1].strip() if ":" in txt else txt,
                "decision": _guess_decision(txt),
                "codes": _codes(txt),
                "skip_to_step": _skip_to(txt),
                "routing_label": "",
                "referenced_sops": [],
                "annotations": [],
                "source_html": str(li),
            })

    # Referenced SOP links
    for a in td.find_all("a", href=True):
        txt = a.get_text(strip=True)
        if _looks_like_doc_ref(txt):
            step["referenced_sops"].append(txt)

    return step


# ─────────────────────────────────────────────────────────────────────────────
# 6.  HTMLDecisionTableAgent
# ─────────────────────────────────────────────────────────────────────────────

def html_decision_tables(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Attach 2-col IF→THEN inner tables to steps.  Detection is purely by
    header text — no class names."""
    soup = _soup(state)
    if not soup:
        return {}
    steps = [dict(s) for s in (state.get("steps") or [])]
    step_by_num = {s["number"]: s for s in steps}

    candidates = _find_step_tables(soup)
    if not candidates:
        return {}
    primary_table, num_col, _ = candidates[0]

    # Only direct rows of the primary table (not nested inner tables)
    data_rows = [r for r in primary_table.find_all("tr")
                 if r.find("td") and r.find_parent("table") == primary_table]
    for tr in data_rows:
        cells = tr.find_all(["td", "th"], recursive=False)
        if num_col >= len(cells):
            continue
        num_txt = cells[num_col].get_text(strip=True)
        if not _is_pure_int(num_txt):
            continue
        num = int(num_txt)
        if num not in step_by_num:
            continue
        content_col = 1 if num_col == 0 else 0
        if content_col >= len(cells):
            continue
        action_td = cells[content_col]

        for inner in action_td.find_all("table"):
            col_count = _table_col_count(inner)
            if col_count != 2:
                continue
            if not _is_if_then_table(inner):
                continue
            rows = _parse_table(inner, is_3col=False)
            step_by_num[num]["decision_rows"].extend(rows)

    return {"steps": list(step_by_num.values())}


# ─────────────────────────────────────────────────────────────────────────────
# 7.  HTMLCompoundTableAgent
# ─────────────────────────────────────────────────────────────────────────────

def html_compound_tables(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Attach 3-col IF/AND/THEN tables to steps."""
    soup = _soup(state)
    if not soup:
        return {}
    steps = [dict(s) for s in (state.get("steps") or [])]
    step_by_num = {s["number"]: s for s in steps}

    candidates = _find_step_tables(soup)
    if not candidates:
        return {}
    primary_table, num_col, _ = candidates[0]

    data_rows = [r for r in primary_table.find_all("tr")
                 if r.find("td") and r.find_parent("table") == primary_table]
    for tr in data_rows:
        cells = tr.find_all(["td", "th"], recursive=False)
        if num_col >= len(cells):
            continue
        num_txt = cells[num_col].get_text(strip=True)
        if not _is_pure_int(num_txt):
            continue
        num = int(num_txt)
        if num not in step_by_num:
            continue
        content_col = 1 if num_col == 0 else 0
        if content_col >= len(cells):
            continue
        action_td = cells[content_col]

        for inner in action_td.find_all("table"):
            col_count = _table_col_count(inner)
            if col_count < 3:
                continue
            if not _is_if_then_table(inner):
                continue
            rows = _parse_table(inner, is_3col=True)
            step_by_num[num]["decision_rows"].extend(rows)

    return {"steps": list(step_by_num.values())}


def _table_col_count(table) -> int:
    """Count columns from the first non-empty row."""
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if cells:
            return sum(int(c.get("colspan", 1)) for c in cells)
    return 0


def _is_if_then_table(table) -> bool:
    """True if first row headers contain if/then keywords.
    Uses prefix/contains matching so 'If Current claim has...' still matches 'if'.
    """
    first_row = table.find("tr")
    if not first_row:
        return False
    headers = [_t(c).lower().strip("…. ") for c in first_row.find_all(["th", "td"])]
    # Use startswith/contains so partial headers like "if current claim has..."
    # still match the canonical keyword set.
    def _matches(h, kw_set):
        return any(h == kw or h.startswith(kw) or kw in h for kw in kw_set)
    has_if   = any(_matches(h, _IF_HEADERS)   for h in headers)
    has_then = any(_matches(h, _THEN_HEADERS)  for h in headers)
    return has_if and has_then


def _parse_table(table, is_3col: bool) -> list[dict]:
    rows = []
    all_rows = table.find_all("tr")
    if not all_rows:
        return rows
    prev_if = ""
    for tr in all_rows[1:]:   # skip header
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        if is_3col and len(cells) >= 3:
            if_t   = _t(cells[0]) or prev_if
            and_t  = _t(cells[1])
            then_t = _t(cells[2])
            if _t(cells[0]):
                prev_if = if_t
        elif len(cells) == 2:
            if_t   = _t(cells[0])
            and_t  = ""
            then_t = _t(cells[1])
            prev_if = if_t
        else:
            if_t   = _t(cells[0])
            and_t  = ""
            then_t = ""
        combined = f"{if_t} {and_t} {then_t}"
        rows.append({
            "condition_if": if_t, "condition_and": and_t, "action": then_t,
            "decision": _guess_decision(then_t),
            "codes": _codes(combined),
            "skip_to_step": _skip_to(then_t),
            "routing_label": "",
            "referenced_sops": [],
            "annotations": [],
            # outerHTML of the if/then <tr>, mirrors what html_dom.py emits
            # so the (:Decision)-[:DERIVED_FROM]->(:HtmlRow) edge can be
            # materialised by neo4j_graph_writer.
            "source_html": str(tr),
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 8.  HTMLGroupTableAgent
# ─────────────────────────────────────────────────────────────────────────────

def _detect_group_table(tag) -> list[dict] | None:
    """Return group-rule rows if `tag` contains a group-rule table, else None."""
    for table in tag.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue
        # Not a step table
        first_col_vals = [r.find("td").get_text(strip=True) if r.find("td") else "" for r in rows[1:]]
        if all(_is_pure_int(v) for v in first_col_vals if v):
            continue
        # Not an if/then table
        if _is_if_then_table(table):
            continue
        col_count = _table_col_count(table)
        if col_count < 2:
            continue
        # Must contain time-period keywords in data cells
        body_text = table.get_text(" ")
        if not _RE_DAYS.search(body_text):
            continue
        return _parse_group_rows(table)
    return None


def _parse_group_rows(table) -> list[dict]:
    all_rows = table.find_all("tr")
    group_rules = []
    for tr in all_rows[1:]:
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells:
            continue
        # colspan row (annotation / section divider)
        if len(cells) == 1 and cells[0].get("colspan"):
            continue
        grp  = _t(cells[0]).strip()
        details = _t(cells[1]).strip() if len(cells) > 1 else ""
        if not grp:
            continue
        gr = {
            "group_name": grp,
            "network_type": "BOTH",
            "limit_days": None, "limit_months": None, "limit_years": None,
            "calculation_from": "DOS",
            "member_submitted_only": "member submitted" in details.lower(),
            "special_notes": [],
            "raw_text": details,
            "is_highlighted": bool(_inline_bg_colour(tr)),
        }
        m = _RE_DAYS.search(details)
        if m:
            val, unit = int(m.group(1)), m.group(2).lower()
            if "day" in unit:       gr["limit_days"]   = val
            elif "month" in unit:   gr["limit_months"] = val
            elif "year" in unit:    gr["limit_years"]  = val
        if len(cells) > 1:
            for li in cells[1].find_all("li"):
                note = _t(li)
                if _NOTE_PREFIX.match(note) or "exception" in note.lower():
                    gr["special_notes"].append(note)
        group_rules.append(gr)
    return group_rules


def html_group_tables(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Detect any group-rule table in the document (not inside step cells —
    those are handled by html_steps via _detect_group_table)."""
    soup = _soup(state)
    if not soup:
        return {}

    step_table_ids = {id(t) for t, _, _ in _find_step_tables(soup)}
    group_rules = []

    for table in soup.find_all("table"):
        if id(table) in step_table_ids:
            continue
        rows = _detect_group_table(table)
        if rows is None:
            continue
        # Avoid already-captured rows from step parsing
        group_rules.extend(rows)

    # Deduplicate by group_name
    seen, out = set(), []
    for gr in group_rules:
        if gr["group_name"] not in seen:
            seen.add(gr["group_name"])
            out.append(gr)

    return {"group_rules": out} if out else {}


# ─────────────────────────────────────────────────────────────────────────────
# 9.  HTMLAnnotationAgent
# ─────────────────────────────────────────────────────────────────────────────

def html_annotations(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Collect all document-level annotations — no class names used."""
    soup = _soup(state)
    if not soup:
        return {}

    anns = []
    seen_texts: set[str] = set()

    def _add(atype, el):
        txt = _t(el)
        if txt and txt not in seen_texts and len(txt) > 8:
            seen_texts.add(txt)
            anns.append({
                "annotation_type": atype,
                "text": txt,
                "is_highlight": atype == "HIGHLIGHT",
            })

    # Highlight colours in inline style
    for el in soup.find_all(True):
        bg = _inline_bg_colour(el)
        atype = _ann_type_from_colour(bg)
        if atype:
            _add(atype, el)

    # blockquote / aside
    for tag_name in ("blockquote", "aside"):
        for el in soup.find_all(tag_name):
            _add("NOTE", el)

    # Note:/Alert:/Warning: text prefix in paragraphs and spans
    for el in soup.find_all(["p", "div", "span", "strong", "em"]):
        txt = _t(el)
        m = _NOTE_PREFIX.match(txt)
        if m:
            kw = m.group(1).upper()
            atype = {"NOTE": "NOTE", "ALERT": "ALERT", "WARNING": "ALERT",
                     "EXCEPTION": "EXCEPTION", "TIP": "TIP",
                     "IMPORTANT": "NOTE"}.get(kw, "NOTE")
            _add(atype, el)

    return {"annotations": anns} if anns else {}


# ─────────────────────────────────────────────────────────────────────────────
# 10.  HTMLReferenceTableAgent
# ─────────────────────────────────────────────────────────────────────────────

_VALID_RE   = re.compile(r"\bvalid\b", re.I)
_INVALID_RE = re.compile(r"\binvalid\b", re.I)
_ATTACH_RE  = re.compile(r"\battachment|POTF\b", re.I)
_CODE_RE    = re.compile(r"\bcode\b", re.I)


def html_reference_tables(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Detect reference tables by content keywords — no class names."""
    soup = _soup(state)
    if not soup:
        return {}

    tables = []
    step_table_ids = {id(t) for t, _, _ in _find_step_tables(soup)}

    # ── POTF valid/invalid rows inside any table ──────────────────────────────
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        label_txt = _t(cells[0])
        content_td = cells[1]

        table_type = None
        if _VALID_RE.search(label_txt) and not _INVALID_RE.search(label_txt):
            if _ATTACH_RE.search(label_txt) or _ATTACH_RE.search(_t(content_td)):
                table_type = "POTF_VALID"
        elif _INVALID_RE.search(label_txt):
            table_type = "POTF_INVALID"
        # Row ID fallback
        row_id = tr.get("id", "")
        if "valid-potf" in row_id and "invalid" not in row_id:
            table_type = "POTF_VALID"
        elif "invalid-potf" in row_id:
            table_type = "POTF_INVALID"

        if not table_type:
            continue

        rows = [
            {"label": "", "content": li.get_text(" ", strip=True), "row_type": table_type}
            for li in content_td.find_all("li")
            if not li.find_parent("li")
        ]
        if rows:
            tables.append({"name": label_txt.strip(), "table_type": table_type, "rows": rows})

    # ── Code/terminology lookup tables ────────────────────────────────────────
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        label_txt = _t(cells[0])
        if not _CODE_RE.search(label_txt):
            continue
        content_td = cells[1]
        rows = [
            {"label": "", "content": li.get_text(" ", strip=True), "row_type": "CODE"}
            for li in content_td.find_all("li")
        ]
        if rows:
            tables.append({"name": label_txt.strip(), "table_type": "CODE_LOOKUP", "rows": rows})

    return {"reference_tables": tables} if tables else {}


# ─────────────────────────────────────────────────────────────────────────────
# 11.  HTMLSubProcedureAgent
# ─────────────────────────────────────────────────────────────────────────────

def html_sub_procedures(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Detect secondary numbered step sequences (sub-procedures such as ERB).

    Strategy: _find_step_tables returns ALL integer-sequence tables.  The
    primary one was consumed by html_steps.  Everything else is a sub-procedure.
    The name is inferred from the nearest heading/title element before the table.
    """
    soup = _soup(state)
    if not soup:
        return {}

    all_candidates = _find_step_tables(soup)
    if len(all_candidates) < 2:
        return {}

    # Sort: primary is the one with most steps starting at 1
    def _score(entry):
        _, _, nums = entry
        return (1 if nums[0] == 1 else 0, len(nums))

    all_candidates.sort(key=_score, reverse=True)
    sub_candidates = all_candidates[1:]   # everything except primary

    subs = []
    for table, num_col, nums in sub_candidates:
        # Find the nearest heading before this table
        name = _nearest_heading(soup, table)
        sub = {
            "name": name or f"Sub-Procedure ({nums[0]}–{nums[-1]})",
            "steps": [],
            "entry_condition": "",
        }
        data_rows = [r for r in table.find_all("tr") if r.find("td")]
        seen: set[int] = set()
        for tr in data_rows:
            cells = tr.find_all(["td", "th"], recursive=False)
            if num_col >= len(cells):
                continue
            num_txt = cells[num_col].get_text(strip=True)
            if not _is_pure_int(num_txt):
                continue
            num = int(num_txt)
            if num in seen:
                continue
            seen.add(num)
            content_col = 1 if num_col == 0 else 0
            if content_col >= len(cells):
                continue
            step = _parse_step_cell(num, cells[content_col])
            step["question"] = f"[{name or 'SUB'} Step {num}] " + step.get("question", "").lstrip()
            step["is_sub_procedure"] = True
            sub["steps"].append(step)
        if sub["steps"]:
            subs.append(sub)

    return {"sub_procedures": subs} if subs else {}


def _nearest_heading(soup, table) -> str:
    """Walk backward from table in document order to find the nearest heading."""
    # Flatten all top-level elements
    for prev in table.find_all_previous(["h1", "h2", "h3", "h4", "h5", "div", "span"]):
        txt = prev.get_text(strip=True)
        # Must look like a section title: short, not empty, not a step number
        if txt and 5 < len(txt) < 120 and not _is_pure_int(txt):
            # Strip "back to top" / navigation suffixes
            txt = re.sub(r"[↑↓]\s*(Back|Top|Menu).*", "", txt, flags=re.I).strip()
            if txt:
                return txt
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 12.  HTMLLinkAgent
# ─────────────────────────────────────────────────────────────────────────────

def html_links(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    soup = _soup(state)
    if not soup:
        return {}
    base = state.get("current_url", "")
    _EXT_MAP = {
        ".xlsx": "XLSX", ".xls": "XLSX",
        ".docx": "DOCX", ".doc": "DOCX",
        ".pdf":  "PDF",
    }
    links = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if href in seen:
            continue
        seen.add(href)

        if not href or href == "#":
            if _looks_like_doc_ref(text):
                links.append({"href": "#", "text": text, "link_type": "UNRESOLVED",
                               "resolved_url": None, "is_resolved": False})
        elif href.startswith("#"):
            links.append({"href": urljoin(base, href), "text": text,
                          "link_type": "INTERNAL_ANCHOR",
                          "resolved_url": None, "is_resolved": False})
        else:
            full = urljoin(base, href)
            ext  = Path(urlparse(full).path).suffix.lower()
            lt   = _EXT_MAP.get(ext, "HTML_SOP")
            links.append({"href": full, "text": text, "link_type": lt,
                          "resolved_url": full, "is_resolved": True})

    return {"links": links}
