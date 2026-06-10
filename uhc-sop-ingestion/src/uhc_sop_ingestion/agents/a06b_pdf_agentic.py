"""DEDICATED PDF AGENTIC FLOW — an "army" of PDF-only agents.

This module is the PDF counterpart of the HTML step/enrich pipeline and is kept
DELIBERATELY SEPARATE from it: it never reads the HTML ``step_inventory`` /
``step_checklist`` and never runs the HTML ``step_checklist_reconciler``. PDFs
flow through their own enrich stage (``pdf_enrich`` in graph.py), then rejoin the
shared, format-agnostic backbone (context → validate → narrative → graph
synthesis → writers → auto-build).

The deterministic detector (``a06.pdf_step_inventory``) produces the PDF-private
``pdf_inventory`` context store. The agents below turn that into the same
high-fidelity ``steps`` structure an HTML SOP yields — one node per Step/Action
with clean ``condition → action`` decision rows, proper decision types, refined
questions, grounded codes and resolved Yes/No + skip-to-step routing — so a PDF
auto-builds into a granular, multi-node workflow with rules.

Agents (each ``fn(state, cfg) -> dict`` partial-state update):
  1. pdf_document_profiler   — LLM: title / purpose / SOP type (metadata)
  2. pdf_step_extractor      — LLM (batched + outline memory): inventory → steps
  3. pdf_question_refiner    — LLM: clear active-voice step questions
  4. pdf_decision_normalizer — LLM: normalise conditions + decision types
  5. pdf_code_grounder       — deterministic: codes + timeframes per rule
  6. pdf_routing_resolver    — deterministic: Yes/No branches + skip-to-step
  7. pdf_terminal_marker     — deterministic: flag F3/F4 terminal steps
  8. pdf_quality_gate        — deterministic: guarantee completeness (no gaps)

Every LLM agent degrades gracefully to a deterministic fallback so a PDF is
NEVER reduced to a half-baked / single-node workflow even if the LLM is
unavailable.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

# Shared, format-agnostic utilities (LLM gateway + regex code/decision helpers).
# These are infrastructure, not HTML-specific extraction logic.
from .a07_enrich import _llm_call
from .a03_parse_html import _codes, _guess_decision, _skip_to

logger = logging.getLogger(__name__)

_VALID_DECISIONS = {"DENY", "ALLOW", "BYPASS", "PEND", "WAIVE", "REFER",
                    "STOP", "SYSTEM", "CONDITIONAL", "OVERRIDE", "NOTE",
                    "ELIGIBILITY"}
_TERMINAL_TOKENS = ("(f3)", "(f4)", "process the claim", "save the claim")
_STEP_CHUNK = 5          # steps per LLM call — small enough to never truncate
_YES = re.compile(r"^\s*(yes|meets criteria|if yes)\b", re.I)
_NO = re.compile(r"^\s*(no|does not meet|if no)\b", re.I)


# ── deterministic step/row builders (PDF-private) ────────────────────────────

def _blank_step(num: int, question: str, raw_text: str) -> dict:
    return {
        "number": num, "question": question, "intro_text": "",
        "decision_rows": [], "annotations": [],
        "branch_yes": "", "branch_no": "",
        "skip_to_step_yes": None, "skip_to_step_no": None,
        "referenced_sops": [],
        "is_terminal": any(t in raw_text.lower() for t in _TERMINAL_TOKENS),
        "raw_text": raw_text, "source_html": "",
    }


def _row_from_cells(num: int, cells: list[str]) -> dict | None:
    cells = [c for c in cells if c and c.strip()]
    if cells and cells[0].strip() == str(num):
        cells = cells[1:]
    if not cells:
        return None
    if len(cells) == 1:
        cond_if, cond_and, action = "", "", cells[0]
    elif len(cells) == 2:
        cond_if, cond_and, action = cells[0], "", cells[1]
    else:
        cond_if, cond_and, action = cells[0], cells[1], " ".join(cells[2:])
    joined = " ".join(cells)
    return {
        "condition_if": cond_if[:500], "condition_and": cond_and[:500],
        "action": action[:1000], "decision": _guess_decision(joined),
        "codes": _codes(joined), "skip_to_step": _skip_to(joined),
        "routing_label": "",
    }


def _fallback_step(entry: dict) -> dict:
    """Deterministic step from one pdf_inventory entry (LLM-free safety net)."""
    num = entry["number"]
    step = _blank_step(num, entry.get("title", ""), entry.get("raw_text", ""))
    for i, r in enumerate(entry.get("rows", [])):
        cells = r.get("cells", [])
        # Skip a first row that merely restates the question.
        if i == 0 and len([c for c in cells if c.strip()
                           and c.strip() != str(num)]) == 1:
            continue
        d = _row_from_cells(num, cells)
        if d:
            step["decision_rows"].append(d)
    return step


def _clean_rows_from_llm(num: int, llm: dict, raw_text: str) -> dict:
    """Build a step dict from a validated LLM step object."""
    step = _blank_step(num, (llm.get("question") or "")[:500], raw_text)
    if isinstance(llm.get("is_terminal"), bool):
        step["is_terminal"] = llm["is_terminal"] or step["is_terminal"]
    step["intro_text"] = str(llm.get("intro_text") or "")[:1000]
    for row in (llm.get("decision_rows") or []):
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "").strip()
        if not action:
            continue
        decision = str(row.get("decision") or "").upper()
        if decision not in _VALID_DECISIONS:
            decision = _guess_decision(action)
        cond_if = str(row.get("condition_if") or "")[:500]
        skip = row.get("skip_to_step")
        try:
            skip = int(skip) if skip is not None else _skip_to(action)
        except (TypeError, ValueError):
            skip = _skip_to(action)
        step["decision_rows"].append({
            "condition_if": cond_if,
            "condition_and": str(row.get("condition_and") or "")[:500],
            "action": action[:1000], "decision": decision,
            "codes": _codes(f"{cond_if} {action}"),
            "skip_to_step": skip, "routing_label": "",
        })
    return step


# ── 1. PDFDocumentProfilerAgent — LLM ────────────────────────────────────────

def pdf_document_profiler(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    if (state.get("doc_format") or "").upper() != "PDF":
        return {}
    raw = (state.get("raw_text") or "")[:4000]
    meta = dict(state.get("metadata") or {})
    if not raw:
        return {}
    prompt = f"""You are a senior claims auditor profiling a healthcare SOP PDF.
Return JSON: {{"title": "...", "purpose": "one sentence", "sop_type": "timely_filing|duplicate|eligibility|coordination_of_benefits|other"}}

Document text (start):
{raw}"""
    res = _llm_call(cfg, prompt, {}, "pdf_document_profiler",
                    provider="anthropic", expected_type=dict,
                    required_keys=["purpose"], max_tokens=512)
    if isinstance(res, dict):
        if res.get("title") and not meta.get("title"):
            meta["title"] = str(res["title"])[:300]
        meta["purpose"] = str(res.get("purpose") or meta.get("purpose", ""))[:500]
        meta["sop_type"] = str(res.get("sop_type") or "")[:64]
        return {"metadata": meta}
    return {}


# ── 2. PDFStepExtractorAgent — LLM (batched, outline memory) ──────────────────

def _extract_chunk(cfg, chunk: list[dict], outline: list[dict]) -> dict[int, dict]:
    payload = [{
        "number": e["number"],
        "title": (e.get("title") or "")[:200],
        "lines": [" | ".join(r["cells"]) for r in e.get("rows", [])][:60],
    } for e in chunk]

    prompt = f"""You convert a healthcare claims SOP (extracted from a PDF) into structured audit steps.

CONTEXT — the full step outline of this SOP (use for routing/skip references, do NOT re-output these):
{json.dumps(outline, indent=2)}

For EACH step below return exactly one object — never skip or merge a step number:
{{
  "number": <same step number>,
  "question": "<the step's question or instruction in clear active voice>",
  "intro_text": "<optional one-line context, else ''>",
  "is_terminal": true|false  (true only for final process/save actions like F3/F4),
  "decision_rows": [{{
     "condition_if":  "<IF condition; '' for an unconditional action>",
     "condition_and": "<AND condition; '' if none>",
     "action":        "<THEN action the auditor must take — complete and specific>",
     "decision":      "DENY|ALLOW|BYPASS|PEND|WAIVE|REFER|STOP|SYSTEM|CONDITIONAL",
     "skip_to_step":  <step number to jump to, or null>
  }}]
}}

Rules:
- "lines" are raw PDF rows; reconstruct If/And/Then or Yes/No branches from them.
- A line that only restates the question is NOT a decision row; header lines (If/And/Then) are NOT rows.
- Preserve every code (EX CODE OCA, F3, W46…) and timeframe (90 days, 365 days…) verbatim in the action.
- Capture "skip to Step N" / "proceed to Step N" as skip_to_step.

Return a JSON array, one object per input step.

Steps:
{json.dumps(payload, indent=2)}"""

    res = _llm_call(cfg, prompt, [], "pdf_step_extractor",
                    provider="anthropic", expected_type=list,
                    required_keys=["number"], max_tokens=8192)
    out: dict[int, dict] = {}
    if isinstance(res, list):
        for r in res:
            if not isinstance(r, dict):
                continue
            try:
                out[int(r.get("number"))] = r
            except (TypeError, ValueError):
                continue
    return out


def pdf_step_extractor(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    inv = state.get("pdf_inventory") or []
    if not inv:
        return {}
    outline = [{"number": e["number"], "title": (e.get("title") or "")[:120]}
               for e in inv]

    by_num: dict[int, dict] = {}
    for i in range(0, len(inv), _STEP_CHUNK):
        chunk = inv[i:i + _STEP_CHUNK]
        try:
            by_num.update(_extract_chunk(cfg, chunk, outline))
        except Exception as exc:  # never let one bad batch sink the whole doc
            logger.warning("pdf_step_extractor: chunk %d failed: %s", i, exc)

    steps: list[dict] = []
    for e in inv:
        num = e["number"]
        llm = by_num.get(num)
        if llm and isinstance(llm.get("decision_rows"), list):
            step = _clean_rows_from_llm(num, llm, e.get("raw_text", ""))
            if not step["question"]:
                step["question"] = (e.get("title") or "")[:500]
            # LLM produced no usable rows → deterministic safety net.
            if not step["decision_rows"]:
                fb = _fallback_step(e)
                fb["question"] = step["question"] or fb["question"]
                step = fb
        else:
            step = _fallback_step(e)
        steps.append(step)

    steps.sort(key=lambda s: s.get("number", 0))
    logger.info("pdf_step_extractor: built %d steps (%d via LLM)",
                len(steps), len(by_num))
    return {"steps": steps}


# ── 3. PDFQuestionRefinerAgent — LLM ─────────────────────────────────────────

def pdf_question_refiner(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("steps") or []
    if not steps or (state.get("doc_format") or "").upper() != "PDF":
        return {}
    payload = [{"number": s["number"], "question": s.get("question", "")}
               for s in steps]
    prompt = f"""Rewrite each SOP step question as a clear, decision-oriented question
or instruction in active voice (keep it faithful — do not invent content).
Return a JSON array of {{"number": N, "question": "..."}}.

Steps:
{json.dumps(payload, indent=2)}"""
    res = _llm_call(cfg, prompt, [], "pdf_question_refiner",
                    provider="anthropic", expected_type=list,
                    required_keys=["number"], max_tokens=4096)
    if not isinstance(res, list):
        return {}
    refined = {}
    for r in res:
        try:
            q = str(r.get("question") or "").strip()
            if q:
                refined[int(r.get("number"))] = q[:500]
        except (TypeError, ValueError):
            continue
    if not refined:
        return {}
    out = [dict(s) for s in steps]
    for s in out:
        if s["number"] in refined:
            s["question"] = refined[s["number"]]
    return {"steps": out}


# ── 4. PDFDecisionNormalizerAgent — LLM ──────────────────────────────────────

def pdf_decision_normalizer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("steps") or []
    if not steps or (state.get("doc_format") or "").upper() != "PDF":
        return {}
    # Only ask the LLM about rows whose decision type is weak/ambiguous.
    weak = []
    for s in steps:
        for j, r in enumerate(s.get("decision_rows", [])):
            if (r.get("decision") or "").upper() not in _VALID_DECISIONS \
               or (r.get("decision") or "").upper() == "CONDITIONAL":
                weak.append({"step": s["number"], "idx": j,
                             "condition": r.get("condition_if", ""),
                             "action": r.get("action", "")})
    if not weak:
        return {}
    prompt = f"""For each claims-audit rule, choose the single best decision type.
Allowed: DENY, ALLOW, BYPASS, PEND, WAIVE, REFER, STOP, SYSTEM, CONDITIONAL.
Return JSON array of {{"step": N, "idx": I, "decision": "TYPE"}}.

Rules:
{json.dumps(weak[:60], indent=2)}"""
    res = _llm_call(cfg, prompt, [], "pdf_decision_normalizer",
                    provider="anthropic", expected_type=list,
                    required_keys=["step"], max_tokens=2048)
    if not isinstance(res, list):
        return {}
    fixes: dict[tuple[int, int], str] = {}
    for r in res:
        try:
            d = str(r.get("decision") or "").upper()
            if d in _VALID_DECISIONS:
                fixes[(int(r["step"]), int(r["idx"]))] = d
        except (TypeError, ValueError, KeyError):
            continue
    if not fixes:
        return {}
    out = [dict(s) for s in steps]
    for s in out:
        rows = [dict(r) for r in s.get("decision_rows", [])]
        for j, r in enumerate(rows):
            if (s["number"], j) in fixes:
                r["decision"] = fixes[(s["number"], j)]
        s["decision_rows"] = rows
    return {"steps": out}


# ── 5. PDFCodeGrounderAgent — deterministic ──────────────────────────────────

def pdf_code_grounder(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("steps") or []
    if not steps or (state.get("doc_format") or "").upper() != "PDF":
        return {}
    out = [dict(s) for s in steps]
    for s in out:
        rows = []
        for r in s.get("decision_rows", []):
            r = dict(r)
            text = f"{r.get('condition_if','')} {r.get('condition_and','')} {r.get('action','')}"
            if not r.get("codes"):
                r["codes"] = _codes(text)
            if r.get("skip_to_step") is None:
                r["skip_to_step"] = _skip_to(text)
            rows.append(r)
        s["decision_rows"] = rows
    return {"steps": out}


# ── 6. PDFRoutingResolverAgent — deterministic ───────────────────────────────

def pdf_routing_resolver(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("steps") or []
    if not steps or (state.get("doc_format") or "").upper() != "PDF":
        return {}
    out = [dict(s) for s in steps]
    for s in out:
        for r in s.get("decision_rows", []):
            cond = r.get("condition_if", "") or ""
            if _YES.match(cond):
                s["branch_yes"] = r.get("action", "")
                if r.get("skip_to_step"):
                    s["skip_to_step_yes"] = r["skip_to_step"]
            elif _NO.match(cond):
                s["branch_no"] = r.get("action", "")
                if r.get("skip_to_step"):
                    s["skip_to_step_no"] = r["skip_to_step"]
    return {"steps": out}


# ── 7. PDFTerminalMarkerAgent — deterministic ────────────────────────────────

def pdf_terminal_marker(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("steps") or []
    if not steps or (state.get("doc_format") or "").upper() != "PDF":
        return {}
    out = [dict(s) for s in steps]
    for s in out:
        blob = f"{s.get('question','')} {s.get('raw_text','')}".lower()
        if any(t in blob for t in _TERMINAL_TOKENS):
            s["is_terminal"] = True
    return {"steps": out}


# ── 8. PDFQualityGateAgent — deterministic completeness guarantee ─────────────

def pdf_quality_gate(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    inv = state.get("pdf_inventory") or []
    if not inv or (state.get("doc_format") or "").upper() != "PDF":
        return {}
    steps = list(state.get("steps") or [])
    have = {s.get("number") for s in steps}
    missing = [e for e in inv if e["number"] not in have]
    # validation_warnings is an additive accumulator → return ONLY new entries.
    new_warnings: list[str] = []
    if missing:
        # Materialise any step the extractor dropped so the canvas is complete.
        for e in missing:
            steps.append(_fallback_step(e))
        steps.sort(key=lambda s: s.get("number", 0))
        new_warnings.append(
            f"pdf_quality_gate: recovered {len(missing)} missing step(s): "
            f"{[e['number'] for e in missing]}")
    empties = [s["number"] for s in steps
               if not s.get("decision_rows") and not s.get("is_terminal")]
    if empties:
        new_warnings.append(
            f"pdf_quality_gate: steps with no rules (non-terminal): {empties}")
    logger.info("pdf_quality_gate: %d steps, %d recovered, %d empty-non-terminal",
                len(steps), len(missing), len(empties))
    result: dict[str, Any] = {}
    if new_warnings:
        result["validation_warnings"] = new_warnings
    if missing:
        result["steps"] = steps
    return result
