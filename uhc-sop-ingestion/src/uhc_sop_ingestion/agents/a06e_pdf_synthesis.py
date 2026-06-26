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

from .a07_enrich import _llm_call, _llm_call_pdf, _llm_call_images
from .a06c_pdf_perception import ctx_read, ctx_write, render_pdf_page_images
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

_STEP_SCHEMA_HINT = json.dumps(
    {
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
        "decision_rows": [
            {
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
                "is_out_of_scope": "bool — true ONLY when this row says the line/claim is "
                "OUT OF SCOPE / not in scope so the engine SKIPS it (no defect, no EOB). A "
                "'stop'/terminal disposition is NOT out of scope — that posts a defect with "
                "an EOB code, so leave this false",
                "subrules": "list of nested decision rows (same shape) when a row has "
                "sub-rules / sub-cases, else []",
            }
        ],
    },
    indent=2,
)


def _gather_step_text(
    root_ids: list[str], by_id: dict, kids_all: dict[str, list[str]]
) -> str:
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
    for c in row.get("subrules") or row.get("children") or []:
        cc = _clean_row(c)
        if cc:
            children.append(cc)
    oos = row.get("is_out_of_scope")
    if not isinstance(oos, bool):
        # Backstop: detect the SOP's own out-of-scope phrasing in this row's text
        # even when the model omitted the flag (paraphrased actions still match).
        blob = " ".join(
            _t(k) for k in ("condition_if", "condition_and", "action", "output_text")
        ).lower()
        # OUT OF SCOPE means the engine SKIPS the line (no defect/EOB). A "stop"
        # is a terminal disposition that posts a defect WITH an EOB code, so it
        # is NOT out of scope and must not match here.
        oos = any(
            m in blob
            for m in (
                "out of scope",
                "out-of-scope",
                "not in scope",
                "no longer in scope",
            )
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


def _deterministic_rows(
    step_id: str, by_id: dict, kids_struct: dict[str, list[str]], goto: dict[str, int]
) -> list[dict]:
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
        rows.append(
            {
                "condition_if": (
                    text[:1000] if e["type"] in {"CONDITION", "WARNING", "NOTE"} else ""
                ),
                "condition_and": "",
                "action": text[:1000] if e["type"] in {"SUBSTEP", "ROW"} else "",
                "output_text": "",
                "applicable_when": "",
                "skip_to_step": goto.get(cid),
                "is_out_of_scope": any(
                    m in text.lower()
                    for m in (
                        "out of scope",
                        "out-of-scope",
                        "not in scope",
                        "no longer in scope",
                    )
                ),
                "subrules": _deterministic_rows(cid, by_id, kids_struct, goto),
            }
        )
    return rows


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


# ── 1a. direct whole-document reconstruction (any PDF, no slicing) ────────────
# The robust default: hand the WHOLE PDF to the LLM in one pass and let it
# reconstruct the numbered procedure(s) directly. Native-PDF (Claude) reads the
# text layer faithfully even when a document is one oversized page or has a
# malformed page tree — the cases that defeat page-by-page slicing. When the
# native read comes back empty (scanned/image-only PDFs), we rasterise to page/
# band IMAGES and retry with an OpenAI gpt-4o multimodal call. Either way the
# output is mapped into the SAME canonical ``steps`` the graph-walk produces, so
# IR synthesis / persistence / the canvas are unchanged.

_DIRECT_ROW_FIELDS = json.dumps(
    {
        "table_name": "verbatim title/heading of the decision table this row belongs "
        "to (short descriptor if untitled); rows from different tables "
        "MUST keep their own table_name",
        "condition_if": "first condition column cell verbatim, else ''",
        "condition_and": "second condition column cell verbatim (fold any further "
        "condition columns here with their labels), else ''",
        "action": "the FULL result/'Then' cell verbatim — every bullet, note, code "
        "and routing phrase; never summarise or truncate",
        "output_text": "any Met/Not-Met or resulting disposition text, else ''",
        "applicable_when": "a guard/qualifier scoping when this row applies, else ''",
        "skip_to_step": "int|null — target step number if this row routes to another "
        "step (go/skip/proceed/continue to step N)",
        "is_out_of_scope": "bool — true ONLY when the row says the line/claim is OUT OF "
        "SCOPE / not in scope so the engine SKIPS it (no defect, no EOB). A 'stop'/terminal "
        "disposition posts a defect with an EOB code and is NOT out of scope — leave false",
        "subrules": "list of nested rows (same shape) for sub-cases, else []",
    }
)

_DIRECT_SCHEMA_HINT = (
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
    f'      "decision_rows": [{_DIRECT_ROW_FIELDS}]\n'
    "    }]\n  }]\n}"
)


def _direct_prompt(text_layer: str) -> str:
    aid = ""
    if text_layer.strip():
        aid = (
            "\n\nA text-layer extraction of the document is provided below to AID "
            "you (it may be imperfect or out of order — the attached document is "
            "the source of truth, but use this to avoid missing any wording):\n"
            "<<<TEXT_LAYER\n" + text_layer[:60000] + "\nTEXT_LAYER>>>"
        )
    return f"""You are reconstructing the numbered procedure(s) of ONE claims-audit
SOP directly from the attached document (page images and/or the PDF). Read the
ENTIRE document top to bottom — it may be a single very tall page or many image
tiles; read ALL of it. Transcribe verbatim; NEVER summarise, paraphrase, simplify
a table into a generic yes/no pair, invent rows, or duplicate content.

COMPLETENESS CONTRACT (this is the most important instruction):
Do NOT miss a SINGLE rule, sub-rule, or sub-sub-rule. Capture every row of every
decision table, every bullet and sub-bullet, every "Note:"/"Alert:", every code,
and every list entry — exactly as printed. Preserve the FULL nesting depth: when a
bullet has sub-bullets which have their own sub-bullets, mirror that hierarchy
with ``subrules`` inside ``subrules`` to whatever depth the document shows. Use
the VISUAL layout (indentation, bullet level, table cell boundaries, columns) to
decide nesting — never flatten nested content and never drop a deeper level. If
you are unsure whether something is a rule, INCLUDE it.

A document may contain MORE THAN ONE independently-numbered "Step/Action"
procedure (e.g. a main review Step/Action table AND a separate "Emergency Response
Bulletins" Step/Action table). Return EACH as its own procedure object with its
own 1..N numbering — never merge two procedures' numbers.

For each procedure list EVERY numbered step IN ORDER with no gaps:
  • Recover any step number the layout makes ambiguous from document order and the
    SOP's own routing language ("Proceed to next step", "Skip to Step N").
  • INCLUDE short final-action steps (e.g. "(F3) Process the claim", "Resolve any
    warning/error messages", "(F4) Save the claim").
  • Put EVERY decision-table row in ``decision_rows`` with its full verbatim result
    cell; set ``skip_to_step`` when a row routes to another step.
  • Put this step's non-table guidance in ``context`` (verbatim, one item per line).
  • CAPTURE OPERATIVE IDENTIFIER LISTS AS SUB-RULES. When a step (or a note in it)
    relies on an explicit list of identifiers that gate the procedure — e.g.
    provider TINs/NPIs, provider names, group/plan names, or codes that are
    INCLUDED IN or EXCLUDED FROM the process (such as a "Virgin Island Providers
    excluded from cross-billing/duplicate" TIN/Provider table) — you MUST keep it.
    Represent it as a decision row whose ``condition_if`` states the gate (e.g.
    "Provider TIN is on the excluded Virgin Island Providers list") and whose
    ``action`` states the effect, and put EACH list entry as a ``subrules`` row
    carrying the identifier verbatim (e.g. condition_if="128380004", action="NAYER,
    ANNE D"). Never silently drop such a list — it changes claim handling.
  • EXCLUDE ONLY non-operative material: the main-menu/table-of-contents, the
    revision-history table, the business-details footer, and pure code/terminology
    DEFINITION glossaries (lists that merely define what a code/term means and do
    not themselves gate the procedure).{aid}

Return STRICT JSON matching:
{_DIRECT_SCHEMA_HINT}
"""


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        import io as _io
        from pypdf import PdfReader

        reader = PdfReader(_io.BytesIO(pdf_bytes), strict=False)
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("_extract_pdf_text failed: %s", exc)
        return ""


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
    """Flatten reconstructed procedures into the canonical ``steps`` list. The
    primary procedure keeps its 1..N numbers; each later (sub-)procedure is
    appended with a running offset so step-number namespaces never collide, and
    its steps are tagged so the canvas can show which procedure they belong to."""
    procs = sorted(
        (p for p in procedures if isinstance(p, dict)),
        key=lambda p: (not p.get("is_primary"),),
    )
    steps_out: list[dict] = []
    offset = 0
    for pi, proc in enumerate(procs):
        is_primary = bool(proc.get("is_primary")) or pi == 0
        pname = str(proc.get("name") or "").strip()
        psteps = sorted(
            (
                s
                for s in (proc.get("steps") or [])
                if isinstance(s, dict) and isinstance(s.get("step_number"), int)
            ),
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


def _direct_pdf_steps(state: "PipelineState", cfg: "PipelineConfig") -> list[dict]:
    """Whole-PDF direct reconstruction. Returns canonical steps, or [] to signal
    the caller to fall back to graph-walk synthesis."""
    b64 = state.get("raw_bytes_b64") or ""
    if not b64:
        return []
    import base64 as _b64

    try:
        pdf_bytes = _b64.b64decode(b64)
    except Exception:
        pdf_bytes = b""

    # Source-of-truth text: PREFER the faithful per-band perceived page text from
    # the shared context (Redis) — multimodal band perception reads dense overview
    # tables (e.g. the excluded-TIN list) that the raw pypdf text layer drops and
    # that whole-document image downscaling can blur. Fall back to the pypdf layer
    # only when no perceived context exists yet.
    perceived = ""
    try:
        pages = ctx_read(cfg, state.get("job_id", ""), "pages", default=[]) or []
        perceived = "\n\n".join(
            _page_to_text(p) for p in pages if isinstance(p, dict)
        ).strip()
    except Exception:
        perceived = ""
    text_layer = perceived or (_extract_pdf_text(pdf_bytes) if pdf_bytes else "")
    prompt = _direct_prompt(text_layer)
    procs: list[dict] = []

    # 1) PRIMARY: multimodal page images (OpenAI gpt-4o). Vision sees the actual
    #    indentation / bullet levels / table cells, so it captures deep
    #    sub-sub-rule nesting and dense operative lists (e.g. excluded-TIN tables)
    #    that flat text extraction loses. Tall pages are band-split into readable
    #    image tiles first. The text layer is also passed to the prompt as an aid.
    if pdf_bytes:
        images = render_pdf_page_images(pdf_bytes)
        if images:
            logger.info(
                "pdf_direct: reading %d page image tile(s) via OpenAI multimodal",
                len(images),
            )
            res = _llm_call_images(
                cfg,
                prompt,
                images,
                fallback={"procedures": []},
                agent_name="pdf_direct_reconstructor_vision",
                provider="openai",
                expected_type=dict,
                required_keys=["procedures"],
                stage="pdf_synthesize",
                max_tokens=16384,
            )
            if isinstance(res, dict):
                procs = res.get("procedures") or []

    # 2) FALLBACK: native-PDF (Claude) — used if rasterisation is unavailable or
    #    the multimodal pass returned nothing.
    if not _procedures_have_steps(procs):
        logger.info(
            "pdf_direct: multimodal empty/unavailable — falling back to native PDF read"
        )
        res2 = _llm_call_pdf(
            cfg,
            prompt,
            [b64],
            fallback={"procedures": []},
            agent_name="pdf_direct_reconstructor",
            expected_type=dict,
            required_keys=["procedures"],
            stage="pdf_synthesize",
            max_tokens=16384,
        )
        if isinstance(res2, dict):
            procs = res2.get("procedures") or []

    steps = _procedures_to_steps(procs) if _procedures_have_steps(procs) else []
    if steps:
        logger.info(
            "pdf_direct: reconstructed %d step(s) across %d procedure(s)",
            len(steps),
            len(procs),
        )
    return steps


def pdf_step_synthesizer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")

    entities = ctx_read(cfg, job_id, "entities", default=[]) or []
    relations = ctx_read(cfg, job_id, "relations", default=[]) or []
    if not entities:
        # No shared graph context (e.g. perception produced nothing) — fall back
        # to a single whole-document reconstruction so a step list still emerges.
        direct = _direct_pdf_steps(state, cfg)
        return {"steps": direct} if direct else {}

    by_id = _index(entities)
    # Hierarchy from parent_ref (covers recovered entities too); GOTO from rels.
    kids = _children_from_parent_ref(entities)
    goto = _goto_targets(relations, by_id)

    pages = ctx_read(cfg, job_id, "pages", default=[]) or []
    page_map = {
        p.get("page_number"): p for p in pages if isinstance(p.get("page_number"), int)
    }

    # The reconciled, de-duplicated, page-ranged step plan (a06d). Fall back to a
    # deterministic merge-by-number of raw STEP entities if it is unavailable.
    step_plan = ctx_read(cfg, job_id, "step_plan", default=[]) or []
    if not step_plan:
        grouped: dict[int, dict] = {}
        for e in entities:
            if e["type"] == "STEP" and isinstance(e.get("step_number"), int):
                n = e["step_number"]
                grouped.setdefault(
                    n,
                    {
                        "step_number": n,
                        "question": "",
                        "entity_ids": [],
                        "start_page": None,
                        "end_page": None,
                    },
                )
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
        explicit_pages = [
            p
            for p in (planned.get("pages") or [])
            if isinstance(p, int) and p in page_map
        ]
        start, end = planned.get("start_page"), planned.get("end_page")
        if explicit_pages:
            # Holistic reconstruction gave the EXACT page set this step's content
            # spans (often non-contiguous, e.g. a table broken across pages). Add
            # the trailing neighbour so a row that wraps just past the last listed
            # page is still captured; the prompt keeps only THIS step's logic.
            want = sorted(set(explicit_pages) | {explicit_pages[-1] + 1})
            raw = "\n".join(_page_to_text(page_map[p]) for p in want if p in page_map)
        elif isinstance(start, int) and isinstance(end, int) and page_map:
            raw = "\n".join(
                _page_to_text(page_map[p])
                for p in range(start, end + 2)
                if p in page_map
            )
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
     CRITICAL: named bullet lists are content, not decoration — capture EVERY
     item of any "Valid/Invalid", "Accepted/Not accepted", "Included/Excluded",
     or "Eligible/Ineligible" list (e.g. "Valid POTF attachments", "Invalid POTF
     attachments", a case/service-type list) verbatim, preserving its heading as
     the leading item so the two sides never merge. Never summarise or drop a
     list item.
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

• OPERATIVE IDENTIFIER LISTS. When this step (or a note/table in it) relies on an
  explicit list of identifiers that gate the procedure — e.g. provider TINs/NPIs,
  provider names, group/plan names, or codes that are INCLUDED IN or EXCLUDED FROM
  the process (such as a "Virgin Island Providers excluded from cross-billing" TIN/
  Provider table) — you MUST keep every entry. Represent the list as a decision row
  whose ``condition_if`` states the gate (e.g. "Provider TIN is on the excluded
  Virgin Island Providers list") and whose ``action`` states the effect, and put
  EACH list entry as a ``subrules`` row carrying the identifier verbatim (e.g.
  condition_if="128380004", action="NAYER, ANNE D"). Never collapse such a table
  into a single generic yes/no row and never drop it — it changes claim handling.

• OUT OF SCOPE vs STOP — these are DIFFERENT, never conflate them.
  - ``is_out_of_scope`` = true ONLY when the SOP says this line/claim is OUT OF
    SCOPE / not in scope / does not apply, so the execution engine SKIPS it —
    there is NO defect and NO EOB code, the engine simply bypasses this line and
    moves on.
  - A "stop"/"the process ends"/"end the review" disposition is NOT out of
    scope: it is a real terminal outcome that posts a DEFECT WITH an EOB code.
    Keep ``is_out_of_scope`` FALSE for it and preserve its defect/EOB/disposition
    text verbatim in ``action``.

Page content:
{raw[:24000]}

Return STRICT JSON matching:
{_STEP_SCHEMA_HINT}
"""
        result = _llm_call(
            cfg,
            prompt,
            fallback=None,
            agent_name="pdf_step_synthesizer",
            provider="anthropic",
            expected_type=dict,
            required_keys=["decision_rows"],
            stage="pdf_synthesize",
            max_tokens=16384,
            retry_on_truncation=True,
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
            context = [
                str(c).strip() for c in (result.get("context") or []) if str(c).strip()
            ]
            if context:
                step["actions"] = context[:80]
                step["intro_text"] = "\n".join(context)[:8000]
            for r in result.get("decision_rows") or []:
                cr = _clean_row(r)
                if cr:
                    rows.append(cr)

        if not rows:  # deterministic safety net — never emit an empty step
            for mid in member_ids:
                rows.extend(r for r in _deterministic_rows(mid, by_id, kids, goto) if r)

        step["decision_rows"] = rows
        steps.append(step)

    if not steps:
        direct = _direct_pdf_steps(state, cfg)
        if direct:
            logger.info("pdf_step_synthesizer: graph-walk empty — used direct "
                        "reconstruction (%d steps)", len(direct))
            return {"steps": direct}

    logger.info(
        "pdf_step_synthesizer: synthesized %d steps from context graph", len(steps)
    )
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
    seen_section_sigs: set[str] = set()
    for sec in sections:
        items: list[dict] = []
        # Overlapping perception bands re-emit the same SECTION text, so dedup
        # item text within a section (and whole-section duplicates below) — this
        # is what otherwise triplicates the Step-0 exception rules downstream.
        seen_items: set[str] = set()
        for d in _descendants(sec["id"], kids_all):
            e = by_id.get(d, {})
            if e.get("type") == "STEP" and isinstance(e.get("step_number"), int):
                continue
            t = (e.get("text") or "").strip()
            sig = " ".join(t.lower().split())
            if t and sig not in seen_items:
                seen_items.add(sig)
                items.append(
                    {
                        "text": t[:2000],
                        "item_type": "RULE",
                        "codes": [],
                        "sub_items": [],
                    }
                )
        sec_sig = "||".join(sorted(i["text"][:80].lower() for i in items))
        if items and sec_sig in seen_section_sigs:
            continue
        if items:
            seen_section_sigs.add(sec_sig)
            pre.append(
                {
                    "name": (sec.get("label") or sec.get("text") or "Section")[:120],
                    "order": order,
                    "section_id": "",
                    "items": items,
                    "annotations": [],
                    "source_html": "",
                }
            )
            order += 1

    logger.info("pdf_presection_synthesizer: %d named pre-sections", len(pre))
    return {"pre_sections": pre} if pre else {}


# ── 2b. pdf_exception_attacher ────────────────────────────────────────────────

_EXC_DTYPES = {"DENY", "ALLOW", "BYPASS", "OVERRIDE", "ELIGIBILITY"}
# A preamble override/exclusion list (e.g. the excluded-TIN/Provider table) is
# referenced by — and operationally belongs to — the step that introduces the
# provider/identifier matching criteria. These signal tokens locate that step.
_EXC_SIGNAL = (
    "provider",
    "tin",
    "npi",
    "tax identification",
    "cross-billing",
    "cross billing",
    "duplicate",
)


def _norm_txt(s: Any) -> str:
    return " ".join(str(s or "").lower().split())


def dedupe_exception_rules(rules: list[dict]) -> list[dict]:
    """Collapse band-overlap duplicates of preamble exception/override rules.

    A tall provider table is rasterised into several OVERLAPPING perception
    bands, so the same gate rule (and its identifier list) is perceived 2-3
    times — and the LLM paraphrases each occurrence differently ("Provider TIN
    is one of…", "Provider is a Virgin Island Provider…", "claim is from…"), so
    an exact (condition, action) dedup lets every copy through. This collapses
    them with two deterministic mechanisms:

    • Identifier-list rules (carry ``sub_rules``) are clustered by their
      identifier SET. Provider TINs/NPIs are globally unique to a group, so two
      gates that share ANY identifier ARE the same gate regardless of wording;
      their ``sub_rules`` are unioned and de-duplicated by (condition, action).
    • Plain rules (no ``sub_rules``) collapse on exact (condition, action).

    First-appearance order is preserved; for a merged cluster the longest (most
    descriptive) gate wording is kept. Never invents or drops a distinct rule.
    """
    groups: list[dict] = []          # {"ids": set, "rule": dict, "subs": dict}
    plain: dict[tuple, dict] = {}
    order: list[tuple] = []          # ("g", idx) | ("p", key) — output order

    for d in rules:
        subs = [
            sr
            for sr in (d.get("sub_rules") or [])
            if isinstance(sr, dict) and (sr.get("condition") or sr.get("action"))
        ]
        if subs:
            ids = {
                _norm_txt(sr.get("condition"))
                for sr in subs
                if _norm_txt(sr.get("condition"))
            }
            # Two gates are the same group when they share a MAJORITY of the
            # smaller identifier set — robust to partial-band copies, but a lone
            # incidental shared TIN won't collapse two genuinely distinct gates.
            def _same_group(g_ids: set) -> bool:
                if not ids or not g_ids:
                    return False
                shared = len(ids & g_ids)
                return shared >= 0.5 * min(len(ids), len(g_ids))

            hit = next((g for g in groups if _same_group(g["ids"])), None)
            if hit is None:
                hit = {"ids": set(ids), "rule": dict(d), "subs": {}}
                groups.append(hit)
                order.append(("g", len(groups) - 1))
            else:
                hit["ids"] |= ids
                # Keep the most descriptive gate wording across paraphrases.
                if len(_norm_txt(d.get("condition"))) > len(
                    _norm_txt(hit["rule"].get("condition"))
                ):
                    hit["rule"] = dict(d)
            for sr in subs:
                hit["subs"].setdefault(
                    (_norm_txt(sr.get("condition")), _norm_txt(sr.get("action"))), sr
                )
        else:
            key = (_norm_txt(d.get("condition")), _norm_txt(d.get("action")))
            if key not in plain:
                plain[key] = dict(d)
                order.append(("p", key))

    out: list[dict] = []
    for kind, ref in order:
        if kind == "g":
            g = groups[ref]
            r = dict(g["rule"])
            r["sub_rules"] = list(g["subs"].values())
            out.append(r)
        else:
            out.append(plain[ref])
    return out


def pdf_exception_attacher(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Attach preamble exception/override rules (with their nested identifier
    lists, e.g. the excluded Virgin-Island-Providers TIN table) to the numbered
    step that references them — instead of isolating them in a synthetic
    'Step 0 — Pre-Step Exceptions' node. The matched step is the one whose text
    introduces the provider/identifier criteria the exceptions gate.

    Records the attached rule signatures in ``exception_rules_attached`` so the
    Postgres writer skips them (Step 0 is then only built for documents where no
    confident host step exists — preserving completeness for any layout)."""
    if (state.get("doc_format") or "").upper() != "PDF":
        return {}
    steps = state.get("enriched_steps") or state.get("steps") or []
    pre = state.get("pre_sections") or []
    if not steps or not pre:
        return {}

    exc_rules: list[dict] = []
    for ps in pre:
        for r in ps.get("llm_rules") or []:
            if not isinstance(r, dict):
                continue
            dt = (r.get("decision_type") or "").upper()
            if r.get("is_exception") or dt in _EXC_DTYPES:
                exc_rules.append({**r, "_section": ps.get("name", "")})
    if not exc_rules:
        return {}

    def _step_text(s: dict) -> str:
        parts = [s.get("question", ""), s.get("intro_text", "")]
        for row in s.get("decision_rows") or []:
            parts += [
                row.get("condition_if", ""),
                row.get("action", ""),
                row.get("output_text", ""),
            ]
        return _norm_txt(" ".join(str(p) for p in parts))

    scored = []
    for s in steps:
        n = s.get("number")
        if not isinstance(n, int) or n <= 0:
            continue
        t = _step_text(s)
        score = sum(1 for kw in _EXC_SIGNAL if kw in t)
        anchor = "provider" in t and any(
            k in t for k in ("tin", "npi", "tax identification")
        )
        scored.append((anchor, score, -n, s))
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)

    # Require a confident host step (provider+identifier anchor, or several
    # signal hits); otherwise leave the rules for the Step-0 fallback so they
    # are never dropped on documents with a different structure.
    if not scored or not (scored[0][0] or scored[0][1] >= 2):
        logger.info("pdf_exception_attacher: no confident host step — keeping Step 0")
        return {}

    # Collapse band-overlap paraphrase duplicates (and union their identifier
    # lists) before attaching, so the host step never shows the same provider
    # gate / TIN three times.
    exc_rules = dedupe_exception_rules(exc_rules)

    target = scored[0][3]
    rows = target.setdefault("decision_rows", [])
    existing = {_norm_txt(r.get("condition_if", "")) for r in rows}
    attached: list[str] = []
    for r in exc_rules:
        cond = str(r.get("condition", "")).strip()
        sig = _norm_txt(cond)
        attached.append(sig)
        if sig in existing:
            continue
        subrules = [
            {
                "condition_if": str(sr.get("condition", "")).strip(),
                "action": str(sr.get("action", "")).strip(),
                "subrules": [],
            }
            for sr in (r.get("sub_rules") or [])
            if isinstance(sr, dict) and (sr.get("condition") or sr.get("action"))
        ]
        rows.append(
            {
                "table_name": r.get("_section") or "Exceptions & Override Rules",
                "condition_if": cond,
                "condition_and": "",
                "action": str(r.get("action", "")).strip(),
                "output_text": "",
                "is_out_of_scope": False,
                "subrules": subrules,
            }
        )
        existing.add(sig)

    logger.info(
        "pdf_exception_attacher: attached %d exception rule(s) to step %s",
        len(attached),
        target.get("number"),
    )
    key = "enriched_steps" if state.get("enriched_steps") else "steps"
    return {key: steps, "exception_rules_attached": attached}


# ── 3. pdf_quality_gate ───────────────────────────────────────────────────────


def _load_sop_ir():
    try:
        from sop_ir.schema import SopIR

        return SopIR
    except Exception as exc:  # standalone CLI without Django project root
        logger.warning(
            "pdf_quality_gate: sop_ir unavailable (%s) — skipping "
            "Pydantic validation",
            exc,
        )
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
    expected_nums = {
        s["step_number"] for s in step_plan if isinstance(s.get("step_number"), int)
    }
    built_nums = {s.get("number") for s in steps if isinstance(s.get("number"), int)}
    missing_steps = sorted(expected_nums - built_nums)

    warnings = list(state.get("validation_warnings") or [])
    if missing_steps:
        warnings.append(
            f"PDF synthesis: STEP node(s) not materialised: {missing_steps}"
        )

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

    ctx_write(
        cfg,
        job_id,
        "synthesis_quality",
        {
            "planned_steps": sorted(expected_nums),
            "built_steps": sorted(n for n in built_nums if n is not None),
            "missing_steps": missing_steps,
            "pre_section_count": len(pre),
            "pydantic_ok": pydantic_ok,
        },
    )
    logger.info(
        "pdf_quality_gate: %d steps, %d pre-sections, missing=%s, pydantic_ok=%s",
        len(steps),
        len(pre),
        missing_steps,
        pydantic_ok,
    )
    return {"validation_warnings": warnings} if warnings else {}
