"""a06d_pdf_context_graph.py — Graph-first contextualization (Phase 2 of PDF door).

Takes the merged, cross-page-coherent perception from a06c and builds a DURABLE
context graph in Neo4j BEFORE any rule extraction. This is the "contextualize all
pages into a graph, then continue" requirement: entities (sections, steps,
sub-steps, conditions, warnings, tables, rows, codes, references) and their
relations (HAS_STEP, HAS_SUBSTEP, HAS_CONDITION, CONTINUES, GOTO, REFERENCES,
APPLIES_TO, IN_SECTION) are reasoned over the WHOLE document, so context is
retained across page breaks and nothing is orphaned.

No hardcoded templates: every entity and relation comes from an LLM call over the
perceived content. The only deterministic code is plumbing (batching, id
re-keying, Neo4j MERGE, coverage counting).

Agents (each ``fn(state, cfg) -> dict``):
  1. ``pdf_entity_extractor``    — page-batched LLM -> flat entity list w/ provenance.
  2. ``pdf_relation_reasoner``   — whole-document LLM -> typed relations between entities.
  3. ``pdf_context_graph_writer``— MERGE the entities/relations into Neo4j.
  4. ``pdf_context_validator``   — coverage gate: every page represented, every
                                   step/substep wired; one critique-retry on gaps.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .a07_enrich import _llm_call
from .a06c_pdf_perception import ctx_read, ctx_write

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)

_ENTITY_TYPES = {
    "SECTION",
    "STEP",
    "SUBSTEP",
    "CONDITION",
    "WARNING",
    "NOTE",
    "TABLE",
    "ROW",
    "CODE",
    "REFERENCE",
}
_REL_TYPES = {
    "HAS_STEP",
    "HAS_SUBSTEP",
    "HAS_CONDITION",
    "HAS_ROW",
    "IN_SECTION",
    "CONTINUES",
    "GOTO",
    "REFERENCES",
    "APPLIES_TO",
}
_PAGE_BATCH = 4  # pages per entity-extraction call — small so output never truncates


# ── page serialization (perception -> compact text the reasoner reads) ────────


def _page_to_text(page: dict) -> str:
    lines: list[str] = []
    num = page.get("page_number")
    lines.append(f"=== PAGE {num} ===")
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


# ── 1. pdf_entity_extractor ───────────────────────────────────────────────────

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
    "Step / Action" procedure table (i.e. a line that literally begins with a step
    integer in a Step column). Do NOT assign a step_number to narrative bullets,
    exception/notes content, or section preambles that merely resemble a step —
    leave their step_number null. A continuation of a step's table on a later page
    is part of that SAME step number, not a new one.

Content:
{body[:60000]}

Return STRICT JSON matching:
{_ENTITY_SCHEMA_HINT}
"""
    result = _llm_call(
        cfg,
        prompt,
        fallback={"entities": []},
        agent_name="pdf_entity_extractor",
        provider="anthropic",
        expected_type=dict,
        required_keys=["entities"],
        stage="pdf_contextualize",
        max_tokens=16384,
    )
    raw = (result.get("entities") if isinstance(result, dict) else None) or []

    # Re-key ids to be globally unique across batches; remap parent_ref too.
    out: list[dict] = []
    idmap: dict[str, str] = {}
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            continue
        local = str(e.get("id") or f"x{i}")
        gid = f"{batch_tag}_{local}"
        idmap[local] = gid
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            continue
        local = str(e.get("id") or f"x{i}")
        etype = str(e.get("type") or "").upper()
        if etype not in _ENTITY_TYPES:
            etype = "NOTE"
        parent_local = str(e.get("parent_ref") or "")
        out.append(
            {
                "id": idmap.get(local, f"{batch_tag}_{local}"),
                "type": etype,
                "label": str(e.get("label") or "")[:200],
                "text": str(e.get("text") or "")[:4000],
                "page": e.get("page"),
                "step_number": e.get("step_number"),
                "parent_ref": idmap.get(parent_local, "") if parent_local else "",
            }
        )
    return out


def pdf_entity_extractor(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    pages = ctx_read(cfg, job_id, "pages", default=[]) or []
    if not pages:
        return {}

    entities: list[dict] = []
    for start in range(0, len(pages), _PAGE_BATCH):
        batch = pages[start : start + _PAGE_BATCH]
        tag = f"b{start // _PAGE_BATCH}"
        entities.extend(_extract_entities_for_pages(cfg, batch, tag))

    ctx_write(cfg, job_id, "entities", entities)
    logger.info(
        "pdf_entity_extractor: %d entities across %d pages", len(entities), len(pages)
    )
    return {}


# ── 2. pdf_relation_reasoner ──────────────────────────────────────────────────

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


def pdf_relation_reasoner(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    entities = ctx_read(cfg, job_id, "entities", default=[]) or []
    if not entities:
        return {}

    # Seed structural edges deterministically from parent_ref so the graph is
    # never empty even if the LLM under-delivers; the LLM then ADDS cross-page
    # and semantic edges (CONTINUES/GOTO/REFERENCES/APPLIES_TO).
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
                rel = (
                    "HAS_CONDITION"
                    if etype in {"CONDITION", "WARNING", "NOTE"}
                    else "IN_SECTION"
                )
            relations.append(
                {"source": parent, "target": e["id"], "rel": rel, "label": ""}
            )

    brief = [
        {
            "id": e["id"],
            "type": e["type"],
            "page": e.get("page"),
            "step_number": e.get("step_number"),
            "text": (e.get("text") or "")[:200],
        }
        for e in entities
    ]

    prompt = f"""You are a claims-audit routing reasoner. Below is the entity list
extracted from one SOP (ids, types, page, step_number, text). Infer the
relationships BETWEEN entities — especially the ones that span page breaks:

  • CONTINUES   : an entity continues content from another (cross-page).
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
        cfg,
        prompt,
        fallback={"relations": []},
        agent_name="pdf_relation_reasoner",
        provider="anthropic",
        expected_type=dict,
        required_keys=["relations"],
        stage="pdf_contextualize",
        max_tokens=16384,
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
                relations.append(
                    {
                        "source": src,
                        "target": tgt,
                        "rel": rel,
                        "label": str(r.get("label") or "")[:200],
                    }
                )

    ctx_write(cfg, job_id, "relations", relations)
    logger.info(
        "pdf_relation_reasoner: %d relations (%d structural seeds + LLM)",
        len(relations),
        len(relations) - len(llm_rels) if llm_rels else len(relations),
    )
    return {}


# ── 3. pdf_context_graph_writer ───────────────────────────────────────────────


def _neo4j(cfg):
    from ..config import get_neo4j

    return get_neo4j(cfg)


def pdf_context_graph_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
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
                MERGE (d:PdfDoc {job_id:$job, content_hash:$ch})
                SET d.title=$title, d.url=$url, d.page_count=$pc
                """,
                job=job_id,
                ch=content_hash,
                title=title,
                url=url,
                pc=state.get("pdf_page_count", 0),
            )
            # Nodes — a single :PdfNode label + a `type` property keeps the write
            # APOC-free and portable; queries filter on n.type.
            sess.run(
                """
                UNWIND $rows AS row
                MERGE (n:PdfNode {job_id:$job, eid:row.id})
                SET n.type=row.type, n.label=row.label, n.text=row.text,
                    n.page=row.page, n.step_number=row.step_number,
                    n.content_hash=$ch
                WITH n
                MATCH (d:PdfDoc {job_id:$job, content_hash:$ch})
                MERGE (d)-[:HAS_NODE]->(n)
                """,
                rows=entities,
                job=job_id,
                ch=content_hash,
            )
            if relations:
                sess.run(
                    """
                    UNWIND $rels AS rel
                    MATCH (a:PdfNode {job_id:$job, eid:rel.source})
                    MATCH (b:PdfNode {job_id:$job, eid:rel.target})
                    MERGE (a)-[r:PDF_REL {rel:rel.rel}]->(b)
                    SET r.label=rel.label
                    """,
                    rels=relations,
                    job=job_id,
                )
        logger.info(
            "pdf_context_graph_writer: wrote %d nodes / %d edges to Neo4j",
            len(entities),
            len(relations),
        )
        return {"pdf_context_graph_id": f"{job_id}:{content_hash}"}
    except Exception as exc:
        logger.warning(
            "pdf_context_graph_writer: Neo4j write failed (%s) — "
            "context graph still lives on the Redis blackboard",
            exc,
        )
        return {}


# ── 4. pdf_context_validator ──────────────────────────────────────────────────


def pdf_context_validator(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    pages = ctx_read(cfg, job_id, "pages", default=[]) or []
    entities = ctx_read(cfg, job_id, "entities", default=[]) or []
    if not pages:
        return {}

    page_nums = {
        p.get("page_number") for p in pages if isinstance(p.get("page_number"), int)
    }
    covered = {e.get("page") for e in entities if isinstance(e.get("page"), int)}
    missing = sorted(n for n in page_nums if n not in covered)

    warnings = list(state.get("validation_warnings") or [])
    if missing:
        logger.warning(
            "pdf_context_validator: %d page(s) had no entities: %s — " "re-extracting",
            len(missing),
            missing,
        )
        # One critique-retry: re-extract ONLY the uncovered pages and append.
        retry_pages = [p for p in pages if p.get("page_number") in set(missing)]
        extra: list[dict] = []
        for start in range(0, len(retry_pages), _PAGE_BATCH):
            batch = retry_pages[start : start + _PAGE_BATCH]
            extra.extend(
                _extract_entities_for_pages(cfg, batch, f"retry{start // _PAGE_BATCH}")
            )
        if extra:
            entities = entities + extra
            ctx_write(cfg, job_id, "entities", entities)
            covered = {
                e.get("page") for e in entities if isinstance(e.get("page"), int)
            }
            missing = sorted(n for n in page_nums if n not in covered)

    if missing:
        warnings.append(
            f"PDF context graph: {len(missing)} page(s) still without "
            f"entities after retry: {missing}"
        )

    coverage_ok = not missing
    ctx_write(
        cfg,
        job_id,
        "context_validation",
        {
            "page_count": len(page_nums),
            "covered_pages": len(covered),
            "missing_pages": missing,
            "entity_count": len(entities),
            "coverage_ok": coverage_ok,
        },
    )
    logger.info(
        "pdf_context_validator: coverage_ok=%s (%d/%d pages, %d entities)",
        coverage_ok,
        len(covered),
        len(page_nums),
        len(entities),
    )
    return {"validation_warnings": warnings} if warnings else {}


# ── 5. pdf_step_reconciler ────────────────────────────────────────────────────
# The PDF analog of the HTML `step_checklist_reconciler`. Because entities are
# extracted in independent page-batches, ONE real step can surface as several
# STEP fragments (same number, different pages) and a few non-step bullets can be
# mis-numbered. This agent reasons over the WHOLE-document STEP outline to produce
# the single authoritative, ordered, de-duplicated step list — merging every
# fragment of a step (listing all its contributing entity ids) and dropping
# entities that are not really part of the numbered Step/Action procedure.


_HOLISTIC_SCHEMA = json.dumps({
    "procedures": [{
        "name": "the procedure's heading verbatim (the section title above its "
                "Step/Action table); '' if unnamed",
        "is_primary": "bool — true for the document's MAIN numbered procedure",
        "steps": [{
            "step_number": "int — the step's number WITHIN THIS procedure",
            "question": "the step's lead instruction/heading, verbatim",
            "pages": "list[int] — EVERY page number whose content (table rows, "
                     "notes, continuations) belongs to this step",
        }],
    }],
}, indent=2)


def _holistic_step_plan(cfg, job_id: str, pages: list[dict]) -> list[dict]:
    """Whole-document step reconstruction (no hardcoding).

    The page-batched extractor cannot hold a multi-page "Step/Action" table
    together: its tall rows surface on later pages as separate untitled If/Then
    tables that LOST the "Step" column, so their owning step number is gone.
    This pass reasons over the ENTIRE perceived document at once and re-threads
    every block/row to its owning step — recovering dropped step numbers from
    the surviving "Step" cells, document order, and the SOP's own "Skip to Step
    N" routing — and records the exact page set each step's content spans. It
    also separates independently-numbered sub-procedures (e.g. an "Emergency
    Response Bulletins" Step/Action table) so their numbers never collide with
    the main procedure's; non-primary procedures are appended with an offset.
    """
    ordered = sorted((p for p in pages if isinstance(p.get("page_number"), int)),
                     key=lambda p: p["page_number"])
    if not ordered:
        return []
    doc = "\n\n".join(_page_to_text(p) for p in ordered)
    page_nums = {p["page_number"] for p in ordered}

    prompt = f"""You are reconstructing the numbered procedure(s) of ONE
claims-audit SOP from its already-perceived, page-by-page content. The
perception was done page by page, so a single multi-page "Step / Action" table
is often BROKEN into separate untitled If/Then tables on later pages that LOST
their "Step" column — you must re-thread those orphaned rows back to the step
they belong to.

A document may contain MORE THAN ONE numbered procedure (e.g. a main Step/Action
table plus a separate, independently-numbered sub-procedure such as an
"Emergency Response Bulletins" Step/Action table). Treat each as its own
procedure with its own 1..N numbering — never merge two procedures' numbers.

For EACH procedure, list EVERY numbered step in order with NO gaps:
  • Recover step numbers the perception dropped, using (a) the visible "Step"
    column integers that survived, (b) document order, and (c) the SOP's
    explicit routing language ("Proceed to next step", "Skip to Step N").
  • INCLUDE short final-action steps that have no table (e.g. "(F3) Process the
    claim", "Resolve any warning/error messages", "(F4) Save the claim").
  • For each step, list in ``pages`` EVERY page whose content belongs to it — a
    step's decision-table rows frequently continue across several (even
    non-adjacent) pages; include all of them.
  • EXCLUDE non-procedure tables (table-of-contents / main-menu, revision
    history, business details, code/terminology lists).
  • But DO keep — on the step they belong to — operative bullet lists that gate
    the procedure (e.g. "Valid/Invalid POTF attachments", "Eligible/Ineligible
    services", excluded-provider lists). Add their page to that step's ``pages``;
    they are decision content, not terminology.
  • Keep ``question`` verbatim.

Perceived document (pages delimited by '=== PAGE n ==='):
{doc[:120000]}

Return STRICT JSON matching:
{_HOLISTIC_SCHEMA}
"""
    res = _llm_call(
        cfg, prompt, fallback={"procedures": []},
        agent_name="pdf_holistic_step_reconstructor", provider="anthropic",
        expected_type=dict, required_keys=["procedures"],
        stage="pdf_contextualize", max_tokens=8192,
    )
    procs = (res.get("procedures") if isinstance(res, dict) else None) or []
    if not procs:
        return []

    # Primary procedure keeps its own 1..N numbers; later procedures are appended
    # with a running offset so step-number namespaces never collide.
    procs_sorted = sorted(procs, key=lambda p: (not p.get("is_primary"),))
    plan: list[dict] = []
    offset = 0
    for pi, proc in enumerate(procs_sorted):
        # ONLY the first procedure owns the base 1..N namespace. Every later
        # procedure is offset, regardless of how the LLM flagged ``is_primary``
        # — if the model mislabels two procedures as primary, deferring to that
        # flag would give both a 1.. range and the global-number merge below
        # would silently overwrite the first procedure's tail. Deciding primacy
        # by position (post primary-first sort) makes collisions impossible.
        is_primary = pi == 0
        proc_name = str(proc.get("name") or "")[:200]
        steps = sorted(
            (s for s in (proc.get("steps") or [])
             if isinstance(s, dict) and isinstance(s.get("step_number"), int)),
            key=lambda s: s["step_number"],
        )
        local_max = 0
        for s in steps:
            pgs = sorted({pp for pp in (s.get("pages") or []) if pp in page_nums})
            if not pgs:
                continue
            n_local = s["step_number"]
            local_max = max(local_max, n_local)
            n_global = n_local if is_primary else offset + n_local
            plan.append({
                "step_number": n_global,
                "question": str(s.get("question") or "")[:500],
                "pages": pgs,
                "start_page": pgs[0],
                "end_page": pgs[-1],
                "entity_ids": [],
                "procedure": proc_name,
                "is_primary": is_primary,
                "local_step_number": n_local,
            })
        offset = (local_max if is_primary else offset + local_max)

    # Collapse any duplicate global numbers, unioning their page sets.
    merged: dict[int, dict] = {}
    for s in plan:
        n = s["step_number"]
        if n in merged:
            merged[n]["pages"] = sorted(set(merged[n]["pages"] + s["pages"]))
            merged[n]["start_page"] = merged[n]["pages"][0]
            merged[n]["end_page"] = merged[n]["pages"][-1]
        else:
            merged[n] = s
    return [merged[n] for n in sorted(merged)]


def _deterministic_step_plan(step_entities: list[dict]) -> list[dict]:
    """LLM-free fallback: merge STEP entities by number. The shortest, most
    header-like text per number becomes the question; all ids are kept."""
    by_num: dict[int, list[dict]] = {}
    for e in step_entities:
        by_num.setdefault(e["step_number"], []).append(e)
    plan: list[dict] = []
    for num in sorted(by_num):
        frags = by_num[num]
        primary = min(frags, key=lambda e: len(e.get("text") or e.get("label") or ""))
        plan.append(
            {
                "step_number": num,
                "question": (primary.get("label") or primary.get("text") or "")[:500],
                "entity_ids": [e["id"] for e in frags],
            }
        )
    return plan


def pdf_step_reconciler(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    entities = ctx_read(cfg, job_id, "entities", default=[]) or []

    # Preferred path: whole-document reconstruction over the verbatim perception.
    # It re-threads orphaned rows to their owning step and records each step's
    # full page span — the only reliable way to recover a multi-page Step/Action
    # table whose later rows lost their "Step" column. The entity-graph path
    # below is kept as a fallback for when perception pages are unavailable or
    # the holistic pass under-delivers.
    pages_ctx = ctx_read(cfg, job_id, "pages", default=[]) or []
    detected_nums = {
        e["step_number"] for e in entities
        if e.get("type") == "STEP" and isinstance(e.get("step_number"), int)
    }
    if pages_ctx:
        holistic = _holistic_step_plan(cfg, job_id, pages_ctx)
        holistic_nums = {s["step_number"] for s in holistic}
        # Accept the holistic plan when it is at least as complete as what the
        # extractor confidently numbered (it normally recovers far more).
        if holistic and not (detected_nums - holistic_nums):
            ctx_write(cfg, job_id, "step_plan", holistic)
            logger.info(
                "pdf_step_reconciler[holistic]: %d steps %s",
                len(holistic),
                [(s["step_number"], s.get("pages")) for s in holistic],
            )
            return {}
        logger.warning(
            "pdf_step_reconciler: holistic plan incomplete (got %s, detected %s)"
            " — falling back to entity-graph reconciliation",
            sorted(holistic_nums), sorted(detected_nums),
        )

    # Consider EVERY STEP-typed entity — NOT only those that already carry an int
    # step_number. The page-batched extractor routinely recognises a step's
    # heading text as a STEP entity yet fails to copy the integer out of the
    # "Step" column (tall multi-page rows, or a number rendered in a separate
    # cell), leaving step_number=None. Dropping those here is precisely what made
    # whole steps — including the document's largest decision table — vanish and
    # forced bogus gap-filled phantom steps downstream. We instead hand the LLM
    # the FULL step outline (detected numbers AND un-numbered headings) and let
    # it rebuild the authoritative, contiguous numbering from document order and
    # the SOP's own "proceed to / skip to Step N" routing language.
    step_entities = [e for e in entities if e.get("type") == "STEP"]
    if not step_entities:
        return {}

    outline = [
        {
            "id": e["id"],
            "page": e.get("page"),
            "detected_step_number": (
                e["step_number"] if isinstance(e.get("step_number"), int) else None
            ),
            "text": (e.get("label") or e.get("text") or "")[:240],
        }
        for e in step_entities
    ]

    prompt = f"""You are reconciling the numbered procedure steps of ONE claims-audit
SOP. Below is every entity tagged as a STEP across all pages (read in independent
page-batches), each with the step number the extractor DETECTED (``detected_step_number``,
which is null when the extractor missed the integer), plus its page and text.

Two batching artefacts you MUST repair:
  • FRAGMENTS — a single real step may appear as MULTIPLE entries (same step,
    different pages). Merge them into one.
  • MISSING NUMBERS — many real steps have ``detected_step_number: null`` because
    the extractor failed to read the integer from the "Step" column (this is
    common for tall rows whose table spans several pages). RECOVER each step's
    true number from: (a) the document/page order of the entries, (b) the
    detected numbers that anchor the sequence, and (c) the SOP's own explicit
    routing language inside the text ("proceed to Step N", "skip to Step N",
    "continue to step N").

Produce the SINGLE authoritative, ordered, CONTIGUOUS list of this document's
numbered procedure steps:
  • One entry per real step number, ascending, with NO gaps. Assign a
    step_number to EVERY real step, including ones whose detected number was null.
  • MERGE all fragments of the same step and list every contributing entity id
    in entity_ids.
  • INCLUDE short final-action steps that have no table (e.g. "(F3) Process the
    claim", "(F4) Save the claim", "Resolve any additional warning/error
    messages") — they are real numbered steps.
  • EXCLUDE entries that are NOT part of the numbered Step/Action procedure:
    table-of-contents / main-menu links, section preambles, or notes that merely
    resemble a step. Drop them and never emit the same real step twice.
  • Keep the step's question/title verbatim (pick the clearest fragment).

STEP entities:
{json.dumps(outline, indent=2)[:24000]}

Return STRICT JSON: {{"steps":[{{"step_number":int,"question":str,"entity_ids":[str,...]}}]}}
"""
    result = _llm_call(
        cfg,
        prompt,
        fallback={"steps": []},
        agent_name="pdf_step_reconciler",
        provider="anthropic",
        expected_type=dict,
        required_keys=["steps"],
        stage="pdf_contextualize",
        max_tokens=8192,
    )
    plan = (result.get("steps") if isinstance(result, dict) else None) or []

    valid_ids = {e["id"] for e in step_entities}
    clean: list[dict] = []
    for s in plan:
        if not isinstance(s, dict) or not isinstance(s.get("step_number"), int):
            continue
        ids = [i for i in (s.get("entity_ids") or []) if i in valid_ids]
        clean.append(
            {
                "step_number": s["step_number"],
                "question": str(s.get("question") or "")[:500],
                "entity_ids": ids,
            }
        )

    # Guardrail: every step the extractor was SURE about (had an int
    # detected_step_number) must survive. If the LLM dropped one — or produced
    # nothing — fall back to a deterministic merge of the numbered entities so a
    # confidently-detected step is never lost. (Un-numbered STEP entities can
    # only be recovered by the LLM, so they don't gate this fallback.)
    llm_nums = {s["step_number"] for s in clean}
    numbered_entities = [
        e for e in step_entities if isinstance(e.get("step_number"), int)
    ]
    detected_nums = {e["step_number"] for e in numbered_entities}
    if not clean or (detected_nums - llm_nums):
        logger.warning(
            "pdf_step_reconciler: LLM plan missing detected step(s) %s — using "
            "deterministic merge fallback",
            sorted(detected_nums - llm_nums),
        )
        clean = _deterministic_step_plan(numbered_entities)

    # collapse any accidental duplicate numbers the LLM may still emit
    merged: dict[int, dict] = {}
    for s in clean:
        n = s["step_number"]
        if n in merged:
            merged[n]["entity_ids"] = list(
                dict.fromkeys(merged[n]["entity_ids"] + s["entity_ids"])
            )
        else:
            merged[n] = s

    # Assign each step a PAGE RANGE so the synthesizer can gather its full content
    # straight from the verbatim perception (robust against however the batched
    # extractor fragmented table rows). Contiguous-numbering gaps are filled by
    # page interpolation — no re-extraction needed — so a step the extractor
    # failed to tag is still synthesised from its page window.
    by_id = {e["id"]: e for e in entities}
    pages = ctx_read(cfg, job_id, "pages", default=[]) or []
    page_nums = sorted(
        p.get("page_number") for p in pages if isinstance(p.get("page_number"), int)
    )
    max_page = page_nums[-1] if page_nums else 1

    present = sorted(merged)
    full = list(range(present[0], present[-1] + 1)) if present else []
    page_of: dict[int, int | None] = {}
    for n in full:
        pgs = _entity_pages(merged.get(n, {}).get("entity_ids", []), by_id)
        page_of[n] = min(pgs) if pgs else None
    # forward then backward fill so gap steps inherit a neighbour's page
    last = None
    for n in full:
        if page_of[n] is None:
            page_of[n] = last
        else:
            last = page_of[n]
    nxt = None
    for n in reversed(full):
        if page_of[n] is None:
            page_of[n] = nxt
        else:
            nxt = page_of[n]

    final: list[dict] = []
    for idx, n in enumerate(full):
        start = page_of[n] or 1
        nb = full[idx + 1] if idx + 1 < len(full) else None
        end = page_of[nb] if nb and page_of[nb] else max_page
        if end < start:
            end = start
        final.append(
            {
                "step_number": n,
                "question": (merged.get(n, {}).get("question") or "")[:500],
                "start_page": start,
                "end_page": end,
                "entity_ids": merged.get(n, {}).get("entity_ids", []),
            }
        )

    ctx_write(cfg, job_id, "step_plan", final)
    logger.info(
        "pdf_step_reconciler: %d STEP entities -> %d authoritative steps %s",
        len(step_entities),
        len(final),
        [(s["step_number"], s["start_page"], s["end_page"]) for s in final],
    )
    return {}


def _entity_pages(entity_ids: list[str], by_id: dict) -> list[int]:
    out = []
    for i in entity_ids:
        p = by_id.get(i, {}).get("page")
        if isinstance(p, int):
            out.append(p)
    return out


# ── 6. pdf_reading_context_tracker (OPTIONAL — not wired by default) ──────────
# "Retain context WHILE the page is being read." Walks the merged pages IN ORDER,
# carrying a running context (the step we are currently inside + the tail of its
# still-open content) from one page-window to the next, and assigns every
# line/row/note to its OWNING step at read time, emitting a per-step verbatim text
# map (``step_segments``).
#
# NOTE: This is intentionally NOT in the default pdf_contextualize stage. On
# documents whose page order is NON-LINEAR — a step's intro on one page but its
# detail table several pages later, with another step's detail in between — and
# whose step questions are near-identical (e.g. "find the current claim situation"
# vs "find the current Medicaid claim situation"), a forward/where-am-I walk
# mis-attributes content. The synthesizer's PAGE-RANGE gathering + explicit
# per-step anchor + meaning-scoped prompt (a06e) handles those layouts correctly,
# so it remains the authoritative path. Keep this agent for linear SOPs or future
# experimentation; wire it into graph.py only after validating it beats the
# page-range path on the target corpus.

_SEGMENT_SCHEMA_HINT = json.dumps(
    {
        "segments": [
            {
                "step_number": "int — the numbered step this text belongs to",
                "kind": "question | context | table",
                "text": "verbatim text for this segment — copy every word",
            }
        ],
        "ending_step": "int|null — the step still OPEN at the very end of these pages "
        "(its table/notes continue onto the next page), else null",
    },
    indent=2,
)

_TRACK_WINDOW = 3  # pages per tracking call — small so context stays tight


def pdf_reading_context_tracker(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    pages = ctx_read(cfg, job_id, "pages", default=[]) or []
    step_plan = ctx_read(cfg, job_id, "step_plan", default=[]) or []
    if not pages or not step_plan:
        return {}

    known = [
        {"step_number": s["step_number"], "question": (s.get("question") or "")[:200]}
        for s in sorted(step_plan, key=lambda x: x.get("step_number", 0))
        if isinstance(s.get("step_number"), int)
    ]
    known_nums = {k["step_number"] for k in known}
    if not known_nums:
        return {}

    ordered = sorted(
        (p for p in pages if isinstance(p.get("page_number"), int)),
        key=lambda p: p["page_number"],
    )

    seg_map: dict[int, list[str]] = {}
    current_step: int | None = None
    open_tail = ""

    for start in range(0, len(ordered), _TRACK_WINDOW):
        window = ordered[start : start + _TRACK_WINDOW]
        body = "\n\n".join(_page_to_text(p) for p in window)
        if current_step is not None:
            ctx_line = (
                f"RUNNING CONTEXT: the previous page ended inside Step "
                f'{current_step}, whose content so far ENDS with:\n"""\n'
                f'{open_tail[-800:]}\n"""\nUse this ONLY as a hint: if the '
                "first lines below genuinely continue that same text "
                f"(e.g. a table cell that wrapped onto this page), they "
                f"belong to Step {current_step}. Otherwise attribute them "
                "to whichever step they actually match."
            )
        else:
            ctx_line = "RUNNING CONTEXT: no step is open yet."

        prompt = f"""You are reading a procedural SOP while MAINTAINING running
context across pages. Assign every line, bullet, note and table row on the pages
below to the numbered STEP it belongs to.

{ctx_line}

KNOWN STEPS in this document (number -> lead question):
{json.dumps(known, indent=2)}

This document is NOT necessarily in linear page order — an index/summary table may
list several steps on one page while each step's DETAIL table appears on a later
page, and one step's detail can sit between two others. So you MUST attribute by
MEANING, not by position.

Principles (document-agnostic):
• ATTRIBUTE BY MEANING. For each segment, pick the KNOWN STEP whose lead question
  it matches — it may be an EARLIER step, not the one currently open. Example of
  the reasoning (not literal): a block elaborating a step's own topic (its codes,
  its routing, its notes) belongs to THAT step even if it appears pages later.
• CONTINUATION HINT ONLY. Use the running context to resolve a true wrap (a table
  cell or sentence physically split across the page break). Do not assume a new
  page keeps the previous step open.
• VERBATIM. Copy text exactly — never summarize, drop, merge, paraphrase, or
  invent. Emit as many segments as there are step transitions on these pages.
• kind: 'question' for a step's lead sentence, 'table' for decision-table rows,
  'context' for any other guidance/notes/prose.
• ending_step: the step whose content is genuinely still open (its table/sentence
  is mid-wrap) at the very END of these pages, else null.

Pages:
{body[:30000]}

Return STRICT JSON matching:
{_SEGMENT_SCHEMA_HINT}
"""
        result = _llm_call(
            cfg,
            prompt,
            fallback={"segments": []},
            agent_name="pdf_reading_context_tracker",
            provider="anthropic",
            expected_type=dict,
            required_keys=["segments"],
            stage="pdf_contextualize",
            max_tokens=16384,
        )
        segs = (result.get("segments") if isinstance(result, dict) else None) or []
        for sg in segs:
            if not isinstance(sg, dict):
                continue
            n = sg.get("step_number")
            txt = str(sg.get("text") or "").strip()
            if not isinstance(n, int) or n not in known_nums or not txt:
                continue
            seg_map.setdefault(n, []).append(txt)
            current_step = n

        end_step = result.get("ending_step") if isinstance(result, dict) else None
        if isinstance(end_step, int) and end_step in known_nums:
            current_step = end_step
        if current_step is not None:
            open_tail = "\n".join(seg_map.get(current_step, []))

    step_segments = {str(n): "\n".join(parts) for n, parts in seg_map.items() if parts}
    ctx_write(cfg, job_id, "step_segments", step_segments)
    logger.info(
        "pdf_reading_context_tracker: assigned read-time content to %d/%d " "steps %s",
        len(step_segments),
        len(known_nums),
        sorted(seg_map),
    )
    return {}
