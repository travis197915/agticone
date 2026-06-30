"""a03b_html_graph.py — HTML graph-first door (perception → contextualize → synthesize).

A COMPLETELY SEPARATE analog of the PDF vision door (a06c/a06d/a06e) for HTML
SOPs. It deliberately shares NO code with the PDF agents so the two flows can
evolve independently: changing HTML never touches PDF and vice-versa. The only
shared dependency is the format-agnostic ``_llm_call`` (in a07_enrich) and the
HTML soup helpers (in a03_parse_html).

Design mirror of the PDF door, but driven by the structured DOM instead of page
images (HTML is already structured, so "perception" is deterministic):

  1. ``html_perceive``            — DOM → an ordered ``pages`` blackboard with the
                                    SAME schema PDF perception emits (blocks /
                                    tables / codes). Redis ns ``sop:html:{job}:*``.
  2. ``html_entity_extractor``    — pages → flat entity list (SECTION/STEP/SUBSTEP/
                                    CONDITION/WARNING/NOTE/TABLE/ROW/CODE/REFERENCE)
                                    with parent_ref provenance (LLM).
  3. ``html_relation_reasoner``   — entities → typed relations (structural seeds +
                                    LLM cross/semantic edges).
  4. ``html_context_graph_writer``— durable context graph in Neo4j (:HtmlDoc /
                                    :HtmlNode / HTML_REL) — the HTML twin of the
                                    PDF :PdfDoc/:PdfNode graph.
  5. ``html_context_validator``   — per-page entity-coverage gate + one retry.
  6. ``html_step_synthesizer``    — pages → canonical ``steps`` with FULLY NESTED
                                    ``decision_rows`` (whole-document holistic
                                    reconstruction, same step/row schema the rest
                                    of the pipeline already understands).
  7. ``html_presection_synthesizer`` — SECTION entities → ``pre_sections``.
  8. ``html_quality_gate``        — completeness + draft-SopIR validation.

Everything downstream (context_stage, narrative, a16 graph synthesis, a18 IR
maker/checker, persist_ir, engine, canvas) is the SAME shared tail PDF uses, so
an ingested HTML SOP yields the same node shapes as a PDF SOP.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from .a07_enrich import _llm_call
from .a03_parse_html import _soup, _codes, _NOTE_PREFIX

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)

_CONTEXT_TTL_SECONDS = 24 * 3600
_PAGE_BATCH = 4
_TERMINAL_TOKENS = ("(f3)", "(f4)", "process the claim", "save the claim")

_ENTITY_TYPES = {
    "SECTION", "STEP", "SUBSTEP", "CONDITION", "WARNING", "NOTE",
    "TABLE", "ROW", "CODE", "REFERENCE",
}
_REL_TYPES = {
    "HAS_STEP", "HAS_SUBSTEP", "HAS_CONDITION", "HAS_ROW", "IN_SECTION",
    "CONTINUES", "GOTO", "REFERENCES", "APPLIES_TO",
}


# ── Redis blackboard (sop:html namespace — separate from sop:pdf) ─────────────

def _redis(cfg: "PipelineConfig"):
    from ..config import get_redis
    return get_redis(cfg)


def _html_key(job_id: str, section: str) -> str:
    return f"sop:html:{job_id}:{section}"


def ctx_write(cfg, job_id: str, section: str, payload: Any) -> None:
    try:
        r = _redis(cfg)
        key = _html_key(job_id, section)
        r.set(key, json.dumps(payload, default=str))
        r.expire(key, _CONTEXT_TTL_SECONDS)
    except Exception as exc:
        logger.warning("html ctx_write[%s] failed: %s", section, exc)


def ctx_read(cfg, job_id: str, section: str, default=None):
    try:
        r = _redis(cfg)
        raw = r.get(_html_key(job_id, section))
        return json.loads(raw) if raw else default
    except Exception as exc:
        logger.warning("html ctx_read[%s] failed: %s", section, exc)
        return default


# ── shared local helpers (duplicated, not imported, so PDF stays independent) ─

def _page_to_text(page: dict) -> str:
    lines: list[str] = []
    lines.append(f"=== PAGE {page.get('page_number')} ===")
    for blk in page.get("blocks") or []:
        t = (blk.get("type") or "paragraph").upper()
        txt = (blk.get("text") or "").strip()
        if txt:
            lines.append(f"[{t}] {txt}")
    for ti, tbl in enumerate(page.get("tables") or []):
        title = (tbl.get("title") or "").strip()
        cols = tbl.get("columns") or []
        lines.append(f"[TABLE {ti + 1}{' — ' + title if title else ''}] columns={cols}")
        for ri, row in enumerate(tbl.get("rows") or []):
            cont = " (cont.)" if row.get("continues_from_prev_row") else ""
            lines.append(f"  row{ri + 1}{cont}: {row.get('cells') or []}")
    return "\n".join(lines)


def _index(entities: list[dict]) -> dict[str, dict]:
    return {e["id"]: e for e in entities if isinstance(e, dict) and e.get("id")}


def _children_from_parent_ref(entities: list[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in entities:
        parent = e.get("parent_ref")
        if parent:
            out.setdefault(parent, []).append(e["id"])
    return out


def _descendants(root: str, kids: dict[str, list[str]]) -> list[str]:
    seen: list[str] = []
    stack = list(kids.get(root, []))
    guard = 0
    while stack and guard < 10000:
        guard += 1
        cur = stack.pop(0)
        if cur in seen:
            continue
        seen.append(cur)
        stack.extend(kids.get(cur, []))
    return seen


def _clean_row(row: Any) -> dict | None:
    if not isinstance(row, dict):
        return None

    def _t(k, limit=6000):
        return str(row.get(k) or "")[:limit]

    sk = row.get("skip_to_step")
    try:
        sk = int(sk) if sk not in (None, "", "null") else None
    except (TypeError, ValueError):
        sk = None
    children = []
    for c in row.get("subrules") or row.get("children") or []:
        cc = _clean_row(c)
        if cc:
            children.append(cc)
    oos = row.get("is_out_of_scope")
    if not isinstance(oos, bool):
        blob = " ".join(
            _t(k) for k in ("condition_if", "condition_and", "action", "output_text")
        ).lower()
        oos = any(
            m in blob
            for m in ("out of scope", "out-of-scope", "not in scope", "no longer in scope")
        )
    out = {
        "table_name": _t("table_name", 200),
        "condition_if": _t("condition_if"),
        "condition_and": _t("condition_and"),
        "action": _t("action"),
        "output_text": _t("output_text"),
        "applicable_when": _t("applicable_when"),
        "skip_to_step": sk,
        "is_out_of_scope": bool(oos),
        "subrules": children,
    }
    if not (out["condition_if"] or out["action"] or out["output_text"] or children):
        return None
    return out


def _blank_step(num: int, question: str, raw_text: str) -> dict:
    return {
        "number": num,
        "question": question[:500],
        "intro_text": "",
        "decision_rows": [],
        "annotations": [],
        "branch_yes": "",
        "branch_no": "",
        "skip_to_step_yes": None,
        "skip_to_step_no": None,
        "referenced_sops": [],
        "is_terminal": any(t in (raw_text or "").lower() for t in _TERMINAL_TOKENS),
        "raw_text": raw_text[:4000],
        "source_html": "",
    }


# ══════════════════════════════════════════════════════════════════════════════
# 1. html_perceive — DOM → `pages` blackboard
# ══════════════════════════════════════════════════════════════════════════════

_NOISE_TAGS = {"script", "style", "head", "noscript", "svg"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = {"p", "blockquote", "pre"}
_CONTAINER_TAGS = {"div", "section", "article", "main", "header", "footer",
                   "aside", "td", "th", "tr", "tbody", "thead", "tfoot", "center",
                   "span", "font", "body", "html"}
_PAGE_ELEMENT_BUDGET = 24  # elements per synthetic "page" — keeps a06d-style batches small


def _block_type(text: str) -> str:
    return "note" if _NOTE_PREFIX.match(text) else "paragraph"


def _direct_cells(tr):
    return tr.find_all(["td", "th"], recursive=False)


def _direct_rows(table):
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") == table]


def _is_data_table(table) -> bool:
    """A data table has ≥2 direct rows, no nested <table> inside its direct cells,
    and at least one row with ≥2 cells. Otherwise it's a layout container we
    descend into (so content inside layout tables is still captured)."""
    rows = _direct_rows(table)
    if len(rows) < 2:
        return False
    for tr in rows:
        for c in _direct_cells(tr):
            if c.find("table"):
                return False
    return any(len(_direct_cells(tr)) >= 2 for tr in rows)


def _serialize_table(table) -> dict:
    rows = _direct_rows(table)
    columns: list[str] = []
    out_rows: list[dict] = []
    for i, tr in enumerate(rows):
        cells = _direct_cells(tr)
        cell_txts = [c.get_text(" ", strip=True) for c in cells]
        if not any(cell_txts):
            continue
        is_header = (i == 0 and tr.find("th") is not None)
        if is_header and not columns:
            columns = cell_txts
            continue
        out_rows.append({"cells": cell_txts, "continues_from_prev_row": False})
    return {
        "title": "",
        "columns": columns,
        "continues_from_prev_page": False,
        "continues_to_next_page": False,
        "rows": out_rows,
    }


def _walk_dom(node, elements: list[tuple]) -> None:
    """Append ordered ('block'|'table', payload) tuples in document order.

    Data tables are emitted as table units; layout tables and generic containers
    are descended into; headings / paragraphs / list items become blocks. Each
    leaf is emitted exactly once (we never recurse into the text-bearing leaf
    tags themselves)."""
    for child in node.find_all(recursive=False):
        name = (child.name or "").lower()
        if name in _NOISE_TAGS:
            continue
        if name == "table":
            if _is_data_table(child):
                elements.append(("table", _serialize_table(child)))
            else:
                _walk_dom(child, elements)
        elif name in _HEADING_TAGS:
            txt = child.get_text(" ", strip=True)
            if txt:
                elements.append(("block", {"type": "heading", "text": txt,
                                           "level": int(name[1])}))
        elif name in _BLOCK_TAGS:
            txt = child.get_text(" ", strip=True)
            if txt:
                elements.append(("block", {"type": _block_type(txt),
                                           "text": txt, "level": 0}))
        elif name in ("ul", "ol"):
            for li in child.find_all("li", recursive=False):
                txt = li.get_text(" ", strip=True)
                if txt:
                    elements.append(("block", {"type": "list_item",
                                               "text": txt, "level": 0}))
        elif name in _CONTAINER_TAGS:
            _walk_dom(child, elements)
        # other tags (img, br, hr, a, input, …) carry no SOP block text on their own


def _paginate(elements: list[tuple]) -> list[dict]:
    """Group ordered elements into synthetic pages — a new page starts on a top
    heading (h1/h2/h3) or when the element budget is hit — so each page stays
    small enough for the batched entity extractor and section locality survives."""
    pages: list[dict] = []
    cur_blocks: list[dict] = []
    cur_tables: list[dict] = []
    cur_codes: list[str] = []

    def _flush():
        nonlocal cur_blocks, cur_tables, cur_codes
        if cur_blocks or cur_tables:
            pages.append({
                "page_number": len(pages) + 1,
                "continues_from_prev_page": False,
                "blocks": cur_blocks,
                "tables": cur_tables,
                "codes": list(dict.fromkeys(cur_codes)),
            })
        cur_blocks, cur_tables, cur_codes = [], [], []

    count = 0
    for kind, payload in elements:
        is_major_heading = (
            kind == "block" and payload.get("type") == "heading"
            and payload.get("level", 6) <= 3
        )
        if (is_major_heading and (cur_blocks or cur_tables)) or count >= _PAGE_ELEMENT_BUDGET:
            _flush()
            count = 0
        if kind == "block":
            cur_blocks.append(payload)
            cur_codes.extend(_codes(payload.get("text", "")))
        else:
            cur_tables.append(payload)
            for row in payload.get("rows", []):
                for cell in row.get("cells", []):
                    cur_codes.extend(_codes(cell))
        count += 1
    _flush()
    return pages


def html_perceive(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    soup = _soup(state)
    if not soup:
        return {}
    body = soup.body or soup
    elements: list[tuple] = []
    _walk_dom(body, elements)
    pages = _paginate(elements)
    if not pages:
        return {}
    job_id = state.get("job_id", "")
    ctx_write(cfg, job_id, "pages", pages)
    logger.info("html_perceive: built %d page-record(s) from DOM "
                "(%d block/table elements)", len(pages), len(elements))
    return {"html_page_count": len(pages)}


# ══════════════════════════════════════════════════════════════════════════════
# 2. html_entity_extractor — pages → entities
# ══════════════════════════════════════════════════════════════════════════════

_ENTITY_SCHEMA_HINT = json.dumps(
    {
        "entities": [
            {
                "id": "str — unique within THIS batch (e.g. 'n1','n2')",
                "type": "SECTION | STEP | SUBSTEP | CONDITION | WARNING | NOTE | TABLE | ROW | CODE | REFERENCE",
                "label": "short human label",
                "text": "verbatim text (full, not summarised)",
                "page": "int — page this entity is on",
                "step_number": "int|null — the numbered step this belongs to, if any",
                "parent_ref": "id of the enclosing entity in THIS batch, or '' ",
            }
        ],
    },
    indent=2,
)


def _extract_entities_for_pages(cfg, pages: list[dict], batch_tag: str) -> list[dict]:
    body = "\n\n".join(_page_to_text(p) for p in pages)
    prompt = f"""You are a claims-audit SOP analyst. Below is the verbatim,
already-perceived content of part of an SOP. Identify every meaningful entity and
return it as a flat list. Capture the document's natural hierarchy via parent_ref
(e.g. a SUBSTEP's parent is its STEP; a STEP's parent is its SECTION; a CONDITION
or ROW's parent is the step/table it belongs to).

Rules:
  • Do NOT invent content. Use the verbatim text shown.
  • Capture nested sub-steps and sub-sub-steps as SUBSTEP entities, each pointing
    to its immediate parent via parent_ref.
  • IF/AND/THEN logic, warnings and notes are CONDITION / WARNING / NOTE entities.
  • Every table is a TABLE entity; its data rows are ROW entities (parent_ref =
    the table id).
  • Codes (EOB/EX/denial/CPT/POS/etc.) are CODE entities.
  • step_number: set this ONLY for an entity that is an actual numbered row of a
    "Step / Action" procedure table (a line that literally begins with a step
    integer in a Step column). Do NOT assign a step_number to narrative bullets,
    exception/notes content, or section preambles that merely resemble a step —
    leave their step_number null.

Content:
{body[:60000]}

Return STRICT JSON matching:
{_ENTITY_SCHEMA_HINT}
"""
    result = _llm_call(
        cfg, prompt, fallback={"entities": []},
        agent_name="html_entity_extractor", provider="anthropic",
        expected_type=dict, required_keys=["entities"],
        stage="html_contextualize", max_tokens=16384,
    )
    raw = (result.get("entities") if isinstance(result, dict) else None) or []

    idmap: dict[str, str] = {}
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            continue
        local = str(e.get("id") or f"x{i}")
        idmap[local] = f"{batch_tag}_{local}"
    out: list[dict] = []
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            continue
        local = str(e.get("id") or f"x{i}")
        etype = str(e.get("type") or "").upper()
        if etype not in _ENTITY_TYPES:
            etype = "NOTE"
        parent_local = str(e.get("parent_ref") or "")
        out.append({
            "id": idmap.get(local, f"{batch_tag}_{local}"),
            "type": etype,
            "label": str(e.get("label") or "")[:200],
            "text": str(e.get("text") or "")[:4000],
            "page": e.get("page"),
            "step_number": e.get("step_number"),
            "parent_ref": idmap.get(parent_local, "") if parent_local else "",
        })
    return out


def html_entity_extractor(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    pages = ctx_read(cfg, job_id, "pages", default=[]) or []
    if not pages:
        return {}
    entities: list[dict] = []
    for start in range(0, len(pages), _PAGE_BATCH):
        batch = pages[start: start + _PAGE_BATCH]
        entities.extend(_extract_entities_for_pages(cfg, batch, f"b{start // _PAGE_BATCH}"))
    ctx_write(cfg, job_id, "entities", entities)
    logger.info("html_entity_extractor: %d entities across %d pages",
                len(entities), len(pages))
    return {}


# ══════════════════════════════════════════════════════════════════════════════
# 3. html_relation_reasoner — entities → relations
# ══════════════════════════════════════════════════════════════════════════════

_RELATION_SCHEMA_HINT = json.dumps(
    {
        "relations": [
            {
                "source": "entity id",
                "target": "entity id",
                "rel": "HAS_STEP | HAS_SUBSTEP | HAS_CONDITION | HAS_ROW | IN_SECTION | "
                "CONTINUES | GOTO | REFERENCES | APPLIES_TO",
                "label": "optional guard/branch label, else ''",
            }
        ],
    },
    indent=2,
)


def html_relation_reasoner(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    entities = ctx_read(cfg, job_id, "entities", default=[]) or []
    if not entities:
        return {}

    relations: list[dict] = []
    by_id = {e["id"]: e for e in entities}
    for e in entities:
        parent = e.get("parent_ref")
        if parent and parent in by_id:
            ptype = by_id[parent]["type"]
            etype = e["type"]
            rel = {
                ("SECTION", "STEP"): "HAS_STEP",
                ("STEP", "SUBSTEP"): "HAS_SUBSTEP",
                ("SUBSTEP", "SUBSTEP"): "HAS_SUBSTEP",
                ("TABLE", "ROW"): "HAS_ROW",
            }.get((ptype, etype))
            if rel is None:
                rel = ("HAS_CONDITION" if etype in {"CONDITION", "WARNING", "NOTE"}
                       else "IN_SECTION")
            relations.append({"source": parent, "target": e["id"], "rel": rel, "label": ""})

    brief = [
        {"id": e["id"], "type": e["type"], "page": e.get("page"),
         "step_number": e.get("step_number"), "text": (e.get("text") or "")[:200]}
        for e in entities
    ]
    prompt = f"""You are a claims-audit routing reasoner. Below is the entity list
extracted from one SOP (ids, types, page, step_number, text). Infer the
relationships BETWEEN entities:

  • CONTINUES   : an entity continues content from another.
  • GOTO        : a step/condition routes to another step ("skip to step N",
                  "proceed to step N"). target = that step's entity id.
  • REFERENCES  : cites another section/SOP/table.
  • APPLIES_TO  : a guard/condition applies to a step or branch.
  • HAS_STEP / HAS_SUBSTEP / HAS_CONDITION / HAS_ROW / IN_SECTION : structure.

Only use ids that exist below. Return STRICT JSON matching:
{_RELATION_SCHEMA_HINT}

Entities:
{json.dumps(brief, indent=2)[:48000]}
"""
    result = _llm_call(
        cfg, prompt, fallback={"relations": []},
        agent_name="html_relation_reasoner", provider="anthropic",
        expected_type=dict, required_keys=["relations"],
        stage="html_contextualize", max_tokens=16384,
    )
    llm_rels = (result.get("relations") if isinstance(result, dict) else None) or []
    seen = {(r["source"], r["target"], r["rel"]) for r in relations}
    for r in llm_rels:
        if not isinstance(r, dict):
            continue
        src, tgt = str(r.get("source") or ""), str(r.get("target") or "")
        rel = str(r.get("rel") or "").upper()
        if src in by_id and tgt in by_id and rel in _REL_TYPES:
            key = (src, tgt, rel)
            if key not in seen:
                seen.add(key)
                relations.append({"source": src, "target": tgt, "rel": rel,
                                  "label": str(r.get("label") or "")[:200]})
    ctx_write(cfg, job_id, "relations", relations)
    logger.info("html_relation_reasoner: %d relations", len(relations))
    return {}


# ══════════════════════════════════════════════════════════════════════════════
# 4. html_context_graph_writer — durable context graph in Neo4j (:HtmlDoc/:HtmlNode)
# ══════════════════════════════════════════════════════════════════════════════

def _neo4j(cfg):
    from ..config import get_neo4j
    return get_neo4j(cfg)


def html_context_graph_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    content_hash = state.get("content_hash", "")
    entities = ctx_read(cfg, job_id, "entities", default=[]) or []
    relations = ctx_read(cfg, job_id, "relations", default=[]) or []
    if not entities:
        return {}
    meta = state.get("metadata") or {}
    title = str(meta.get("title", "") or "")
    url = state.get("current_url", "")
    try:
        driver = _neo4j(cfg)
        with driver.session(database=getattr(cfg, "neo4j_database", "neo4j")) as sess:
            sess.run(
                """
                MERGE (d:HtmlDoc {job_id:$job, content_hash:$ch})
                SET d.title=$title, d.url=$url, d.page_count=$pc
                """,
                job=job_id, ch=content_hash, title=title, url=url,
                pc=state.get("html_page_count", 0),
            )
            sess.run(
                """
                UNWIND $rows AS row
                MERGE (n:HtmlNode {job_id:$job, eid:row.id})
                SET n.type=row.type, n.label=row.label, n.text=row.text,
                    n.page=row.page, n.step_number=row.step_number,
                    n.content_hash=$ch
                WITH n
                MATCH (d:HtmlDoc {job_id:$job, content_hash:$ch})
                MERGE (d)-[:HAS_NODE]->(n)
                """,
                rows=entities, job=job_id, ch=content_hash,
            )
            if relations:
                sess.run(
                    """
                    UNWIND $rels AS rel
                    MATCH (a:HtmlNode {job_id:$job, eid:rel.source})
                    MATCH (b:HtmlNode {job_id:$job, eid:rel.target})
                    MERGE (a)-[r:HTML_REL {rel:rel.rel}]->(b)
                    SET r.label=rel.label
                    """,
                    rels=relations, job=job_id,
                )
        logger.info("html_context_graph_writer: wrote %d nodes / %d edges to Neo4j",
                    len(entities), len(relations))
        return {"html_context_graph_id": f"{job_id}:{content_hash}"}
    except Exception as exc:
        logger.warning("html_context_graph_writer: Neo4j write failed (%s) — "
                       "context graph still lives on the Redis blackboard", exc)
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# 5. html_context_validator — per-page entity-coverage gate (one retry)
# ══════════════════════════════════════════════════════════════════════════════

def html_context_validator(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    pages = ctx_read(cfg, job_id, "pages", default=[]) or []
    entities = ctx_read(cfg, job_id, "entities", default=[]) or []
    if not pages:
        return {}
    page_nums = {p.get("page_number") for p in pages if isinstance(p.get("page_number"), int)}
    covered = {e.get("page") for e in entities if isinstance(e.get("page"), int)}
    missing = sorted(n for n in page_nums if n not in covered)
    warnings = list(state.get("validation_warnings") or [])
    if missing:
        retry_pages = [p for p in pages if p.get("page_number") in set(missing)]
        extra: list[dict] = []
        for start in range(0, len(retry_pages), _PAGE_BATCH):
            batch = retry_pages[start: start + _PAGE_BATCH]
            extra.extend(_extract_entities_for_pages(cfg, batch, f"retry{start // _PAGE_BATCH}"))
        if extra:
            entities = entities + extra
            ctx_write(cfg, job_id, "entities", entities)
            covered = {e.get("page") for e in entities if isinstance(e.get("page"), int)}
            missing = sorted(n for n in page_nums if n not in covered)
    if missing:
        warnings.append(f"HTML context graph: {len(missing)} page(s) without entities: {missing}")
    ctx_write(cfg, job_id, "context_validation", {
        "page_count": len(page_nums), "covered_pages": len(covered),
        "missing_pages": missing, "entity_count": len(entities),
        "coverage_ok": not missing,
    })
    logger.info("html_context_validator: coverage_ok=%s (%d/%d pages, %d entities)",
                not missing, len(covered), len(page_nums), len(entities))
    return {"validation_warnings": warnings} if warnings else {}


# ══════════════════════════════════════════════════════════════════════════════
# 6. html_step_synthesizer — pages → canonical steps (holistic reconstruction)
# ══════════════════════════════════════════════════════════════════════════════

_ROW_FIELDS = json.dumps({
    "table_name": "verbatim title/heading of the decision table this row belongs "
    "to (short descriptor if untitled); rows from different tables MUST keep their "
    "own table_name",
    "condition_if": "first condition column cell verbatim, else ''",
    "condition_and": "second condition column cell verbatim (fold any further "
    "condition columns here with their labels), else ''",
    "action": "the FULL result/'Then' cell verbatim — every bullet, note, code and "
    "routing phrase; never summarise or truncate",
    "output_text": "any Met/Not-Met or resulting disposition text, else ''",
    "applicable_when": "a guard/qualifier scoping when this row applies, else ''",
    "skip_to_step": "int|null — target step number if this row routes to another "
    "step (go/skip/proceed/continue to step N)",
    "is_out_of_scope": "bool — true ONLY when the row says the line/claim is OUT OF "
    "SCOPE / not in scope so the engine SKIPS it (no defect, no EOB). A 'stop'/"
    "terminal disposition posts a defect with an EOB code and is NOT out of scope",
    "subrules": "list of nested rows (same shape) for sub-cases, else []",
})

_SYNTH_SCHEMA_HINT = (
    '{\n  "procedures": [{\n'
    '    "name": "the procedure heading verbatim (section title above its '
    "Step/Action table); '' if unnamed\",\n"
    '    "is_primary": "bool — true for the document\'s MAIN numbered procedure",\n'
    '    "steps": [{\n'
    '      "step_number": "int — the step number WITHIN THIS procedure",\n'
    '      "question": "the step\'s lead instruction/heading, verbatim",\n'
    '      "is_terminal": "bool — true only when the step just ends the workflow '
    '(final disposition / process / save) with no further routing",\n'
    '      "context": ["each non-table guidance line of THIS step verbatim '
    '(intro prose, Notes/Alert/definition/checklist bullets), one per item"],\n'
    f'      "decision_rows": [{_ROW_FIELDS}]\n'
    "    }]\n  }]\n}"
)


def _procedures_have_steps(procedures: Any) -> bool:
    if not isinstance(procedures, list):
        return False
    for p in procedures:
        if isinstance(p, dict):
            for s in p.get("steps") or []:
                if isinstance(s, dict) and isinstance(s.get("step_number"), int):
                    return True
    return False


def _procedures_to_steps(procedures: list[dict]) -> list[dict]:
    procs = sorted((p for p in procedures if isinstance(p, dict)),
                   key=lambda p: (not p.get("is_primary"),))
    steps_out: list[dict] = []
    offset = 0
    for pi, proc in enumerate(procs):
        is_primary = bool(proc.get("is_primary")) or pi == 0
        pname = str(proc.get("name") or "").strip()
        psteps = sorted(
            (s for s in (proc.get("steps") or [])
             if isinstance(s, dict) and isinstance(s.get("step_number"), int)),
            key=lambda s: s["step_number"],
        )
        local_max = 0
        for s in psteps:
            n_local = s["step_number"]
            local_max = max(local_max, n_local)
            n_global = n_local if is_primary else offset + n_local
            q = str(s.get("question") or "").strip()
            if not is_primary and pname:
                q = f"[{pname}] {q}"
            ctx = [str(c).strip() for c in (s.get("context") or []) if str(c).strip()]
            raw = "\n".join([q, *ctx])
            step = _blank_step(n_global, q, raw)
            if isinstance(s.get("is_terminal"), bool):
                step["is_terminal"] = s["is_terminal"] or step["is_terminal"]
            if ctx:
                step["actions"] = ctx[:80]
                step["intro_text"] = "\n".join(ctx)[:8000]
            rows: list[dict] = []
            for r in s.get("decision_rows") or []:
                cr = _clean_row(r)
                if cr:
                    rows.append(cr)
            step["decision_rows"] = rows
            step["procedure"] = pname
            step["is_sub_procedure"] = not is_primary
            steps_out.append(step)
        offset = local_max if is_primary else offset + local_max
    return steps_out


def html_step_synthesizer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    pages = ctx_read(cfg, job_id, "pages", default=[]) or []
    if not pages:
        return {}
    doc = "\n\n".join(_page_to_text(p) for p in pages if isinstance(p, dict))
    if not doc.strip():
        return {}

    prompt = f"""You are reconstructing the numbered procedure(s) of ONE claims-audit
SOP from its already-perceived, structured content (a faithful transcription of
the source HTML, pages delimited by '=== PAGE n ==='). Read ALL of it. Transcribe
verbatim; NEVER summarise, paraphrase, simplify a table into a generic yes/no
pair, invent rows, or duplicate content.

COMPLETENESS CONTRACT (the most important instruction):
Do NOT miss a SINGLE rule, sub-rule, or sub-sub-rule. Capture every row of every
decision table, every bullet and sub-bullet, every "Note:"/"Alert:", every code,
and every list entry — exactly as printed. Preserve the FULL nesting depth using
``subrules`` inside ``subrules`` to whatever depth the document shows. If unsure
whether something is a rule, INCLUDE it.

A document may contain MORE THAN ONE independently-numbered "Step/Action"
procedure. Return EACH as its own procedure object with its own 1..N numbering —
never merge two procedures' numbers.

For each procedure list EVERY numbered step IN ORDER with no gaps:
  • Recover any ambiguous step number from document order and routing language
    ("Proceed to next step", "Skip to Step N").
  • INCLUDE short final-action steps (e.g. "(F3) Process the claim", "(F4) Save
    the claim").
  • Put EVERY decision-table row in ``decision_rows`` with its full verbatim
    result cell; set ``skip_to_step`` when a row routes to another step.
  • Put this step's non-table guidance in ``context`` (verbatim, one per line).
  • CAPTURE OPERATIVE IDENTIFIER LISTS AS SUB-RULES. When a step relies on an
    explicit list of identifiers that gate the procedure (provider TINs/NPIs,
    provider names, group/plan names, or included/excluded codes), keep it:
    represent the gate as a decision row and put EACH list entry as a ``subrules``
    row carrying the identifier verbatim. Never silently drop such a list.
  • EXCLUDE ONLY non-operative material: the main-menu/table-of-contents, the
    revision-history table, the business-details footer, and pure code/terminology
    DEFINITION glossaries.
  • OUT OF SCOPE vs STOP are DIFFERENT — never conflate them. ``is_out_of_scope``
    is true ONLY when the SOP says the line/claim is OUT OF SCOPE so the engine
    SKIPS it (no defect, no EOB). A "stop"/terminal disposition posts a DEFECT
    WITH an EOB code — keep ``is_out_of_scope`` false and preserve its text.

Perceived document:
{doc[:120000]}

Return STRICT JSON matching:
{_SYNTH_SCHEMA_HINT}
"""
    res = _llm_call(
        cfg, prompt, fallback={"procedures": []},
        agent_name="html_step_synthesizer", provider="anthropic",
        expected_type=dict, required_keys=["procedures"],
        stage="html_synthesize", max_tokens=16384, retry_on_truncation=True,
    )
    procs = (res.get("procedures") if isinstance(res, dict) else None) or []
    steps = _procedures_to_steps(procs) if _procedures_have_steps(procs) else []
    if steps:
        logger.info("html_step_synthesizer: reconstructed %d step(s) across %d "
                    "procedure(s)", len(steps), len(procs))
        return {"steps": steps}
    logger.info("html_step_synthesizer: no steps reconstructed — leaving DOM-parsed steps")
    return {}


# ══════════════════════════════════════════════════════════════════════════════
# 7. html_presection_synthesizer — SECTION entities → pre_sections
# ══════════════════════════════════════════════════════════════════════════════

def html_presection_synthesizer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    entities = ctx_read(cfg, job_id, "entities", default=[]) or []
    if not entities:
        return {}
    by_id = _index(entities)
    kids_all = _children_from_parent_ref(entities)
    sections = [e for e in entities if e["type"] == "SECTION"]
    pre: list[dict] = []
    order = 0
    seen_section_sigs: set[str] = set()
    for sec in sections:
        items: list[dict] = []
        seen_items: set[str] = set()
        for d in _descendants(sec["id"], kids_all):
            e = by_id.get(d, {})
            if e.get("type") == "STEP" and isinstance(e.get("step_number"), int):
                continue
            t = (e.get("text") or "").strip()
            sig = " ".join(t.lower().split())
            if t and sig not in seen_items:
                seen_items.add(sig)
                items.append({"text": t[:2000], "item_type": "RULE",
                              "codes": [], "sub_items": []})
        sec_sig = "||".join(sorted(i["text"][:80].lower() for i in items))
        if items and sec_sig in seen_section_sigs:
            continue
        if items:
            seen_section_sigs.add(sec_sig)
            pre.append({
                "name": (sec.get("label") or sec.get("text") or "Section")[:120],
                "order": order, "section_id": "", "items": items,
                "annotations": [], "source_html": "",
            })
            order += 1
    logger.info("html_presection_synthesizer: %d named pre-sections", len(pre))
    return {"pre_sections": pre} if pre else {}


# ══════════════════════════════════════════════════════════════════════════════
# 8. html_quality_gate — completeness + draft-SopIR validation
# ══════════════════════════════════════════════════════════════════════════════

def _load_sop_ir():
    try:
        from sop_ir.schema import SopIR
        return SopIR
    except Exception as exc:
        logger.warning("html_quality_gate: sop_ir unavailable (%s) — skipping "
                       "Pydantic validation", exc)
        return None


def html_quality_gate(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    steps = state.get("steps") or []
    pre = state.get("pre_sections") or []
    warnings = list(state.get("validation_warnings") or [])

    pydantic_ok = True
    SopIR = _load_sop_ir()
    if SopIR is not None and steps:
        try:
            from .a18_ir_synthesis import build_draft_ir
            draft = build_draft_ir({**state, "steps": steps, "pre_sections": pre})
            SopIR.model_validate(draft)
        except Exception as exc:
            pydantic_ok = False
            warnings.append(f"HTML synthesis: draft IR failed SopIR validation: {exc}")
            logger.warning("html_quality_gate: SopIR validation failed: %s", exc)

    ctx_write(cfg, job_id, "synthesis_quality", {
        "built_steps": sorted(s.get("number") for s in steps
                              if isinstance(s.get("number"), int)),
        "pre_section_count": len(pre),
        "pydantic_ok": pydantic_ok,
    })
    logger.info("html_quality_gate: %d steps, %d pre-sections, pydantic_ok=%s",
                len(steps), len(pre), pydantic_ok)
    return {"validation_warnings": warnings} if warnings else {}
