"""a06e_pdf_synthesis.py — Graph -> canonical steps/rules (Phase 3 of PDF door).

Walks the context graph built in a06d and synthesizes the SAME ``steps`` /
``pre_sections`` structure an HTML SOP yields, with FULLY NESTED decision rows
(sub-rule / sub-sub-rule), then validates the result against the canonical
Pydantic ``SopIR`` so the PDF door can only ever emit a shape the rest of the
pipeline (a16 graph synthesis, a18 IR maker/checker, persist_ir, engine, UI)
already understands.

Agents (each ``fn(state, cfg) -> dict``):
  1. ``pdf_step_synthesizer``      — numbered STEP nodes + their descendant
                                     conditions/sub-steps -> ``steps`` with
                                     recursive ``decision_rows``. An LLM cleans
                                     each step's verbatim text into condition/
                                     action/output/routing; a deterministic
                                     fallback guarantees a step is never empty.
  2. ``pdf_presection_synthesizer``— SECTION nodes (non-step named content) ->
                                     ``pre_sections`` so nothing is dropped.
  3. ``pdf_quality_gate``          — completeness (every STEP/SECTION node
                                     materialised) + ``SopIR.model_validate``
                                     of the draft IR; flags issues, never crashes.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .a07_enrich import _llm_call
from .a06c_pdf_perception import ctx_read, ctx_write
from .a06d_pdf_context_graph import _page_to_text

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)

_TERMINAL_TOKENS = ("(f3)", "(f4)", "process the claim", "save the claim")


# ── graph helpers ─────────────────────────────────────────────────────────────

def _index(entities: list[dict]) -> dict[str, dict]:
    return {e["id"]: e for e in entities if isinstance(e, dict) and e.get("id")}


def _children_map(relations: list[dict], rels: set[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in relations:
        if r.get("rel") in rels:
            out.setdefault(r.get("source"), []).append(r.get("target"))
    return out


def _children_from_parent_ref(entities: list[dict]) -> dict[str, list[str]]:
    """Hierarchy straight from each entity's ``parent_ref``. This is always
    present (set at extraction time) and so also covers RECOVERED entities that
    never went through the relation reasoner."""
    out: dict[str, list[str]] = {}
    for e in entities:
        parent = e.get("parent_ref")
        if parent:
            out.setdefault(parent, []).append(e["id"])
    return out


def _goto_targets(relations: list[dict], by_id: dict[str, dict]) -> dict[str, int]:
    """source entity id -> target step_number for GOTO edges."""
    out: dict[str, int] = {}
    for r in relations:
        if r.get("rel") == "GOTO":
            tgt = by_id.get(r.get("target"))
            if tgt and isinstance(tgt.get("step_number"), int):
                out[r.get("source")] = tgt["step_number"]
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


# ── 1. pdf_step_synthesizer ───────────────────────────────────────────────────

_STEP_SCHEMA_HINT = json.dumps({
    "question": "the step's LEAD instruction/question in one clean sentence "
                "(verbatim where possible). Do NOT cram the whole step in here.",
    "is_terminal": "bool — true only when the step's sole action ends the "
                   "workflow with no further routing (a final disposition / "
                   "stop / save / submit action)",
    "context": [
        "Each narrative / guidance line that belongs to THIS step but is NOT a "
        "row of a decision table — captured VERBATIM, one array item per line or "
        "bullet, in document order. This covers the step's intro sentence(s), any "
        "'Notes:'/'Alert:'/definition/checklist bullets, and any prose that sits "
        "above, between, or after the table(s). NEVER drop this step's guidance "
        "and NEVER move it into decision_rows. The supplied text usually also "
        "contains neighbouring steps and their notes/continuations — include ONLY "
        "guidance that belongs to this step; do not copy another step's "
        "intro/notes/continuation here.",
    ],
    "decision_rows": [{
        "table_name": "the title/heading of the decision table THIS row belongs "
                      "to, verbatim (use a short descriptor if the table is "
                      "untitled). A step may contain MULTIPLE distinct tables — "
                      "rows from different tables MUST keep their own table_name; "
                      "never merge separate tables into one.",
        "condition_if": "the first condition column cell (verbatim), else ''",
        "condition_and": "the second condition column cell (verbatim) for tables "
                         "with two condition columns; if there are more than two "
                         "condition columns, append the extra ones here verbatim "
                         "with their header labels; else ''",
        "action": "the FULL result/outcome cell (the 'Then'/disposition column), "
                  "VERBATIM — include every bullet, sub-bullet, any 'Note:' text, "
                  "all codes, and any routing instruction. Never summarize, "
                  "truncate, paraphrase, or drop bullets. A result cell may "
                  "continue onto the next page — include that continuation too.",
        "output_text": "any Met/Not-Met or resulting disposition text, else ''",
        "applicable_when": "a guard/qualifier that scopes when this row applies, "
                           "else ''",
        "skip_to_step": "int|null — the target step number if this row routes the "
                        "auditor to another step (go/skip/proceed/continue to step N)",
        "is_out_of_scope": "bool — true ONLY when this row states the line/claim is "
                           "out of scope or that auditing should stop on this path",
        "subrules": "list of nested decision rows (same shape) when a row has "
                    "sub-rules / sub-cases, else []",
    }],
}, indent=2)


def _gather_step_text(root_ids: list[str], by_id: dict, kids_all: dict[str, list[str]]) -> str:
    """Collect the verbatim text of one or more root STEP entities plus all of
    their descendants (sub-steps, conditions, table rows), de-duplicated. Accepts
    MULTIPLE roots so a step that was fragmented across page-batches is gathered
    as a single coherent blob before synthesis."""
    parts: list[str] = []
    seen: set[str] = set()
    for rid in root_ids:
        root = by_id.get(rid, {})
        if rid not in seen and root.get("text"):
            parts.append(root["text"])
            seen.add(rid)
        for d in _descendants(rid, kids_all):
            if d in seen:
                continue
            seen.add(d)
            t = (by_id.get(d, {}).get("text") or "").strip()
            if t:
                parts.append(f"- {t}")
    return "\n".join(parts)


def _clean_row(row: Any) -> dict | None:
    if not isinstance(row, dict):
        return None
    # Generous cap: THEN cells can be long (multiple bullets + a "Note:" + a
    # routing instruction), and the whole point here is to NOT lose any word.
    def _t(k, limit=6000):
        return str(row.get(k) or "")[:limit]
    sk = row.get("skip_to_step")
    try:
        sk = int(sk) if sk not in (None, "", "null") else None
    except (TypeError, ValueError):
        sk = None
    children = []
    for c in (row.get("subrules") or row.get("children") or []):
        cc = _clean_row(c)
        if cc:
            children.append(cc)
    oos = row.get("is_out_of_scope")
    if not isinstance(oos, bool):
        # Backstop: detect the SOP's own out-of-scope phrasing in this row's text
        # even when the model omitted the flag (paraphrased actions still match).
        blob = " ".join(_t(k) for k in
                        ("condition_if", "condition_and", "action", "output_text")).lower()
        oos = any(m in blob for m in
                  ("out of scope", "out-of-scope",
                   "stop further auditing", "stop auditing further"))
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


def _deterministic_rows(step_id: str, by_id: dict, kids_struct: dict[str, list[str]],
                        goto: dict[str, int]) -> list[dict]:
    """LLM-free fallback: each direct child entity becomes a decision row,
    recursing for its own children so nesting survives."""
    rows: list[dict] = []
    for cid in kids_struct.get(step_id, []):
        e = by_id.get(cid)
        if not e:
            continue
        text = (e.get("text") or "").strip()
        if not text:
            continue
        rows.append({
            "condition_if": text[:1000] if e["type"] in {"CONDITION", "WARNING", "NOTE"} else "",
            "condition_and": "",
            "action": text[:1000] if e["type"] in {"SUBSTEP", "ROW"} else "",
            "output_text": "",
            "applicable_when": "",
            "skip_to_step": goto.get(cid),
            "is_out_of_scope": any(
                m in text.lower() for m in
                ("out of scope", "out-of-scope",
                 "stop further auditing", "stop auditing further")),
            "subrules": _deterministic_rows(cid, by_id, kids_struct, goto),
        })
    return rows


def _blank_step(num: int, question: str, raw_text: str) -> dict:
    return {
        "number": num, "question": question[:500], "intro_text": "",
        "decision_rows": [], "annotations": [],
        "branch_yes": "", "branch_no": "",
        "skip_to_step_yes": None, "skip_to_step_no": None,
        "referenced_sops": [],
        "is_terminal": any(t in (raw_text or "").lower() for t in _TERMINAL_TOKENS),
        "raw_text": raw_text[:4000], "source_html": "",
    }


def pdf_step_synthesizer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    entities = ctx_read(cfg, job_id, "entities", default=[]) or []
    relations = ctx_read(cfg, job_id, "relations", default=[]) or []
    if not entities:
        return {}

    by_id = _index(entities)
    # Hierarchy from parent_ref (covers recovered entities too); GOTO from rels.
    kids = _children_from_parent_ref(entities)
    goto = _goto_targets(relations, by_id)

    pages = ctx_read(cfg, job_id, "pages", default=[]) or []
    page_map = {p.get("page_number"): p for p in pages
                if isinstance(p.get("page_number"), int)}

    # The reconciled, de-duplicated, page-ranged step plan (a06d). Fall back to a
    # deterministic merge-by-number of raw STEP entities if it is unavailable.
    step_plan = ctx_read(cfg, job_id, "step_plan", default=[]) or []
    if not step_plan:
        grouped: dict[int, dict] = {}
        for e in entities:
            if e["type"] == "STEP" and isinstance(e.get("step_number"), int):
                n = e["step_number"]
                grouped.setdefault(n, {"step_number": n, "question": "",
                                       "entity_ids": [], "start_page": None, "end_page": None})
                grouped[n]["entity_ids"].append(e["id"])
                if not grouped[n]["question"]:
                    grouped[n]["question"] = e.get("label") or e.get("text") or ""
        step_plan = [grouped[n] for n in sorted(grouped)]

    steps: list[dict] = []
    for planned in sorted(step_plan, key=lambda s: s.get("step_number", 0)):
        num = planned.get("step_number")
        if not isinstance(num, int):
            continue
        member_ids = [i for i in (planned.get("entity_ids") or []) if i in by_id]

        # Gather this step's content from its PAGE RANGE in the verbatim
        # perception — this captures the entire table even when its rows were
        # fragmented across extraction batches, and (unlike a forward page-walk)
        # is robust to documents whose step intro and step detail table sit on
        # non-adjacent pages. We extend the range by ONE trailing page so a THEN
        # cell / table that continues across a page break is still in the blob;
        # the prompt makes the LLM keep only THIS step's logic. Fall back to
        # entity-hierarchy text when no page range is available.
        start, end = planned.get("start_page"), planned.get("end_page")
        if isinstance(start, int) and isinstance(end, int) and page_map:
            raw = "\n".join(_page_to_text(page_map[p]) for p in range(start, end + 2)
                            if p in page_map)
        else:
            raw = _gather_step_text(member_ids, by_id, kids)
        if not raw.strip():
            continue
        step = _blank_step(num, planned.get("question") or "", raw)

        q_label = f": {planned['question']}" if planned.get("question") else ""
        prompt = f"""You are a document-structure synthesizer for procedural SOPs
(any domain, any layout). Below is the verbatim perceived content of the page(s)
that contain Step {num}{q_label}. The text may also include neighbouring steps and
their continuations — reason about the document's structure and extract ONLY the
content that belongs to Step {num}.

GOLDEN RULE: capture every word that belongs to this step, verbatim. Never
summarize, paraphrase, invent, deduplicate, or drop content. Your job is faithful
STRUCTURING, not editing.

Sort this step's content into THREE buckets:
  1. ``question`` — the single lead instruction/heading sentence for the step.
  2. ``context`` — every guidance/narrative line that belongs to this step but is
     not a table row (intro prose, 'Notes'/'Alert'/definition/checklist bullets,
     and any prose above, between, or after the table(s)). One verbatim item per
     line/bullet, in document order. These are real rules — keep them all.
  3. ``decision_rows`` — the rows of the step's decision table(s). Preserve
     nested sub-rules / sub-cases as nested ``subrules``.

Apply these STRUCTURAL principles (they are document-agnostic):

• STEP ATTRIBUTION. The supplied text frequently spans more than one step because
  page ranges overlap. Decide which step each line belongs to by its position
  relative to the step headings, then keep only Step {num}'s material. Do not pull
  a neighbouring step's intro, notes, or trailing continuation into this step.

• MULTIPLE TABLES PER STEP. A step may contain several distinct tables (for
  example a small qualifier/exception table plus a main table). Give each row the
  verbatim ``table_name`` of the table it came from and never merge rows from
  different tables.

• FULL RESULT CELLS. Put the entire result/outcome ("Then"/disposition) cell in
  ``action`` verbatim — every bullet, sub-bullet, note, code, and routing phrase.
  When a routing phrase names a target step (go/skip/proceed/continue to step N),
  also set ``skip_to_step`` to N.

• CONTINUATIONS ACROSS PAGE BREAKS. A cell — usually a result cell — often
  continues after a page break as loose bullets, a note, or a row whose condition
  columns are EMPTY. These are NOT new context and NOT new rows: append them to
  the ``action``/``output_text`` of the row they elaborate, choosing that row by
  MEANING, not merely by position. A row that already reads as self-complete does
  not absorb trailing fragments.

• COLUMN ALIGNMENT. Tables have one or more condition columns followed by a
  result column. Map the first condition column to ``condition_if`` and the second
  to ``condition_and``; fold any further condition columns into ``condition_and``
  with their header labels. If a row's only condition sits in a later column while
  the first column is empty, move that condition into ``condition_if``.

• HIERARCHY. When rows nest (a parent condition with sub-cases beneath it, or a
  result cell that branches into labelled sub-scenarios that the table itself
  presents as separate rows), represent that nesting with ``subrules`` rather than
  flattening — but never fabricate hierarchy the document does not show.

• OUT OF SCOPE. Set ``is_out_of_scope`` true ONLY when the text states that
  line/claim is out of scope or that work should stop on that path.

Page content:
{raw[:24000]}

Return STRICT JSON matching:
{_STEP_SCHEMA_HINT}
"""
        result = _llm_call(
            cfg, prompt, fallback=None,
            agent_name="pdf_step_synthesizer",
            provider="anthropic",
            expected_type=dict,
            required_keys=["decision_rows"],
            stage="pdf_synthesize",
            max_tokens=16384,
        )

        rows: list[dict] = []
        if isinstance(result, dict):
            if result.get("question"):
                step["question"] = str(result["question"])[:500]
            if isinstance(result.get("is_terminal"), bool):
                step["is_terminal"] = result["is_terminal"] or step["is_terminal"]
            # Verbatim non-table guidance (above/between/below the table(s)). These
            # are carried as step-level `actions` so build_draft_ir → plan.py land
            # them in AuditStep.intro_text — nothing the SOP states is lost.
            context = [str(c).strip() for c in (result.get("context") or [])
                       if str(c).strip()]
            if context:
                step["actions"] = context[:80]
                step["intro_text"] = "\n".join(context)[:8000]
            for r in (result.get("decision_rows") or []):
                cr = _clean_row(r)
                if cr:
                    rows.append(cr)

        if not rows:  # deterministic safety net — never emit an empty step
            for mid in member_ids:
                rows.extend(r for r in _deterministic_rows(mid, by_id, kids, goto) if r)

        step["decision_rows"] = rows
        steps.append(step)

    logger.info("pdf_step_synthesizer: synthesized %d steps from context graph",
                len(steps))
    return {"steps": steps}


# ── 2. pdf_presection_synthesizer ─────────────────────────────────────────────

def pdf_presection_synthesizer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    entities = ctx_read(cfg, job_id, "entities", default=[]) or []
    if not entities:
        return {}

    by_id = _index(entities)
    kids_all = _children_from_parent_ref(entities)

    # A SECTION's content = its descendants that are NOT numbered steps (those
    # become `steps`). Sections with only steps under them are pure containers
    # and add no loose rules, so they're skipped here.
    sections = [e for e in entities if e["type"] == "SECTION"]
    pre: list[dict] = []
    order = 0
    for sec in sections:
        items: list[dict] = []
        for d in _descendants(sec["id"], kids_all):
            e = by_id.get(d, {})
            if e.get("type") == "STEP" and isinstance(e.get("step_number"), int):
                continue
            t = (e.get("text") or "").strip()
            if t:
                items.append({"text": t[:2000], "item_type": "RULE",
                              "codes": [], "sub_items": []})
        if items:
            pre.append({
                "name": (sec.get("label") or sec.get("text") or "Section")[:120],
                "order": order, "section_id": "", "items": items,
                "annotations": [], "source_html": "",
            })
            order += 1

    logger.info("pdf_presection_synthesizer: %d named pre-sections", len(pre))
    return {"pre_sections": pre} if pre else {}


# ── 3. pdf_quality_gate ───────────────────────────────────────────────────────

def _load_sop_ir():
    try:
        from sop_ir.schema import SopIR
        return SopIR
    except Exception as exc:  # standalone CLI without Django project root
        logger.warning("pdf_quality_gate: sop_ir unavailable (%s) — skipping "
                       "Pydantic validation", exc)
        return None


def pdf_quality_gate(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    steps = state.get("steps") or []
    pre = state.get("pre_sections") or []

    # Completeness is measured against the AUTHORITATIVE reconciled step plan
    # (a06d.pdf_step_reconciler), which already merged page-split fragments and
    # dropped mis-numbered non-step content. Comparing against raw STEP entities
    # here would falsely flag those intentionally-dropped numbers.
    step_plan = ctx_read(cfg, job_id, "step_plan", default=[]) or []
    expected_nums = {s["step_number"] for s in step_plan
                     if isinstance(s.get("step_number"), int)}
    built_nums = {s.get("number") for s in steps if isinstance(s.get("number"), int)}
    missing_steps = sorted(expected_nums - built_nums)

    warnings = list(state.get("validation_warnings") or [])
    if missing_steps:
        warnings.append(f"PDF synthesis: STEP node(s) not materialised: {missing_steps}")

    # Pydantic-cleanness: the draft IR built from these steps must validate.
    pydantic_ok = True
    SopIR = _load_sop_ir()
    if SopIR is not None:
        try:
            from .a18_ir_synthesis import build_draft_ir
            draft = build_draft_ir({**state, "steps": steps, "pre_sections": pre})
            SopIR.model_validate(draft)
        except Exception as exc:
            pydantic_ok = False
            warnings.append(f"PDF synthesis: draft IR failed SopIR validation: {exc}")
            logger.warning("pdf_quality_gate: SopIR validation failed: %s", exc)

    ctx_write(cfg, job_id, "synthesis_quality", {
        "planned_steps": sorted(expected_nums),
        "built_steps": sorted(n for n in built_nums if n is not None),
        "missing_steps": missing_steps,
        "pre_section_count": len(pre),
        "pydantic_ok": pydantic_ok,
    })
    logger.info("pdf_quality_gate: %d steps, %d pre-sections, missing=%s, pydantic_ok=%s",
                len(steps), len(pre), missing_steps, pydantic_ok)
    return {"validation_warnings": warnings} if warnings else {}
