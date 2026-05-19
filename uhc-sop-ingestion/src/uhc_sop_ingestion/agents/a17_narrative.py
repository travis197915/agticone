"""LLM NARRATIVE-CONTEXT LAYER — 2 agents.

Designed to run AFTER structural extraction (parse + enrich) and BEFORE
the postgres writer, so the persisted ``AuditSop`` and ``AuditStep``
rows carry rich, human-readable narrative paragraphs.

Why
---
The extracted If/Then tables are precise but read like compiled bytecode.
A claims auditor (or a workflow author picking rules for a node)
benefits from a paragraph that says, in plain English:

  • What this step is FOR (purpose).
  • How the auditor walks the row alternatives.
  • Where the flow goes next (terminal / goto / branching).
  • Notable codes or exceptions to watch for.

Agents
------
* ``sop_overview_narrator``  → ``state["sop_narrative"]`` (string)
* ``step_narrative_writer``  → mutates ``state["steps"]`` in place,
  setting ``s["narrative_context"]`` per step.

Both agents reuse the guard-railed ``_llm_call`` dispatcher from
``a07_enrich`` so they get the same retry / fallback / logging machinery
the rest of the pipeline uses.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

from .a07_enrich import _llm_call

logger = logging.getLogger(__name__)


# ── 1. SopOverviewNarrator — Anthropic ────────────────────────────────────────

def sop_overview_narrator(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Write a 5-8 sentence executive narrative for the whole SOP.

    Reads the raw text + metadata + a sketch of preconditions and steps,
    asks Claude for a flowing-prose introduction.  Falls back to a blank
    string on total LLM failure so downstream writers still succeed.
    """
    meta  = state.get("metadata") or {}
    pre   = state.get("pre_sections") or []
    steps = state.get("enriched_steps") or state.get("steps") or []
    raw   = (state.get("raw_text") or "")[:8000]

    if not (raw or steps or pre):
        return {}

    prompt = f"""You are writing the opening narrative for a claims-audit SOP brief.

Write 5-8 sentences (flowing paragraphs, no bullets, no headings) covering:
  1. Purpose & applicability — who this SOP is for and when it kicks in.
  2. High-level walkthrough — the auditor's journey from pre-conditions,
     through the decision-tree steps, to terminal actions.
  3. Notable codes, group rules, or exceptions a reader must remember.

Be concrete. Use specific group names, step numbers, and codes from the
material below. Avoid generic claims-processing platitudes.

== METADATA ==
TITLE:    {meta.get('title', '')}
PLATFORM: {meta.get('platform', '')}
LOB:      {meta.get('lob', [])}

== STRUCTURE ==
PRE-CONDITION SECTIONS: {[s.get('name', '') for s in pre[:10]]}
STEP COUNT: {len(steps)}
FIRST STEPS (by question): {[s.get('question', '')[:120] for s in steps[:5]]}
LAST STEPS  (by question): {[s.get('question', '')[:120] for s in steps[-3:]]}

== ORIGINAL TEXT (first 8000 chars) ==
{raw}

Return JSON: {{"narrative": "your prose here"}}"""

    result = _llm_call(
        cfg, prompt,
        fallback={"narrative": ""},
        agent_name="sop_overview_narrator",
        provider="anthropic",
        expected_type=dict,
        required_keys=["narrative"],
        stage="narrative_stage",
        max_tokens=2048,
    )
    narrative = (result or {}).get("narrative", "") if isinstance(result, dict) else ""
    logger.info("sop_overview_narrator: %d chars", len(narrative))
    return {"sop_narrative": narrative}


# ── 2. StepNarrativeWriter — Anthropic, batched ──────────────────────────────

def step_narrative_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Generate a 2-3 sentence narrative for every step.

    Batches steps so each LLM call stays inside the context window.
    The result is merged into ``state["steps"]`` (and ``enriched_steps``)
    by adding a ``narrative_context`` key to each step dict so that
    ``pg_step_writer`` can persist it in one shot.
    """
    steps = state.get("enriched_steps") or state.get("steps") or []
    if not steps:
        return {}

    meta = state.get("metadata") or {}
    raw  = (state.get("raw_text") or "")[:4000]
    BATCH = 6

    narratives_by_num: dict[int, str] = {}

    for i in range(0, len(steps), BATCH):
        batch = steps[i:i + BATCH]
        compact = []
        for s in batch:
            num = s.get("step_number") or s.get("number")
            if num is None:
                continue
            decisions = (s.get("decision_rows") or s.get("rows") or [])[:10]
            compact.append({
                "number":      num,
                "question":    (s.get("question") or "")[:240],
                "intro":       (s.get("intro_text") or s.get("note") or "")[:400],
                "is_terminal": bool(s.get("is_terminal", False)),
                "decision_rows": [{
                    "if":       (r.get("condition_if") or r.get("if") or "")[:160],
                    "and":      (r.get("condition_and") or r.get("and") or "")[:160],
                    "then":     (r.get("action") or r.get("then") or "")[:280],
                    "goto":     r.get("skip_to_step") or r.get("goto_step"),
                    "decision": r.get("decision") or r.get("decision_type") or "",
                } for r in decisions],
            })

        if not compact:
            continue

        prompt = f"""You write short context paragraphs that explain SOP decision
steps to a claims auditor.

For EACH step below, write a single paragraph of 2-3 sentences that:
  • States the PURPOSE of the step in plain English.
  • Briefly walks through how the auditor evaluates the If/Then rows.
  • Mentions where the flow goes next (terminal, specific goto step,
    or a branching choice) and any notable codes (e.g. W46, F3, EX003)
    that get applied here.

Stay factual — only use information from the data below; do NOT invent
codes, groups, or steps.

Return JSON:
{{"narratives": [{{"number": N, "narrative": "the paragraph"}}, ...]}}

== SOP CONTEXT ==
TITLE:    {meta.get('title', '')}
PLATFORM: {meta.get('platform', '')}

== STEPS TO NARRATE ==
{json.dumps(compact, indent=2)}

== ORIGINAL TEXT (excerpt for grounding) ==
{raw}
"""
        result = _llm_call(
            cfg, prompt,
            fallback={"narratives": []},
            agent_name=f"step_narrative_writer[batch_{i // BATCH}]",
            provider="anthropic",
            expected_type=dict,
            stage="narrative_stage",
            max_tokens=4096,
        )
        items = (result or {}).get("narratives", []) if isinstance(result, dict) else []
        for it in items or []:
            n = it.get("number")
            text = (it.get("narrative") or "").strip()
            if n is None or not text:
                continue
            try:
                narratives_by_num[int(n)] = text
            except (TypeError, ValueError):
                continue

    if not narratives_by_num:
        return {}

    enriched: list[dict] = []
    for s in steps:
        s2 = dict(s)
        num = s.get("step_number") or s.get("number")
        try:
            num_i = int(num) if num is not None else None
        except (TypeError, ValueError):
            num_i = None
        if num_i is not None and num_i in narratives_by_num:
            s2["narrative_context"] = narratives_by_num[num_i]
        enriched.append(s2)

    logger.info("step_narrative_writer: %d/%d steps narrated",
                len(narratives_by_num), len(steps))
    return {"steps": enriched, "enriched_steps": enriched}
