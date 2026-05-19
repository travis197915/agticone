"""LLM ENRICHMENT LAYER — 10 agents.

DESIGN PRINCIPLES
─────────────────
• Zero regex / NLP — every insight comes from an LLM call.
• Dual-provider: OpenAI for structured extraction, Anthropic for
  reasoning/classification.  Each agent declares its preferred provider.
• Guardrails on every call:
    1. OpenAI → response_format=json_object (enforced JSON)
    2. Anthropic → explicit JSON-only instruction + markdown strip
    3. Schema check — validate expected keys/type before accepting output
    4. Retry ×2 with the primary provider, appending the error to the prompt
    5. Cross-provider fallback — if all retries fail, try the other provider
    6. Every attempt (success or failure) logged to Postgres via PipelineLogger

Provider assignment
───────────────────
OpenAI (gpt-4o family) — structured extraction tasks:
    date_condition_extractor, group_rule_extractor,
    pre_section_rule_extractor, cross_reference_resolver

Anthropic (claude-sonnet family) — reasoning / judgment tasks:
    step_question_refiner, decision_row_classifier,
    rule_semantic_enricher, ambiguous_term_resolver,
    potf_validator, summary_generator

Renamed agents (no "NLP" suffix anywhere):
    date_condition_nlp  → date_condition_extractor
    group_rule_nlp      → group_rule_extractor
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)

# ── Provider helpers ──────────────────────────────────────────────────────────

def _make_openai_llm(cfg, max_tokens: int = 4096):
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=cfg.openai_model,
        api_key=cfg.openai_api_key,
        max_tokens=max_tokens,
        model_kwargs={"response_format": {"type": "json_object"}},
    )


def _make_anthropic_llm(cfg, max_tokens: int = 4096):
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=cfg.anthropic_model,
        api_key=cfg.anthropic_api_key,
        max_tokens=max_tokens,
    )


def _strip_markdown(text: str) -> str:
    """Strip ```json ... ``` fences from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # drop first fence line and last fence line
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()
    return text


def _parse_json(text: str) -> Any:
    """Parse JSON, stripping markdown fences first."""
    return json.loads(_strip_markdown(text))


def _token_usage(resp) -> tuple[int, int]:
    usage = getattr(resp, "usage_metadata", None) or {}
    return (usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0),
            usage.get("output_tokens", 0) or usage.get("completion_tokens", 0))


def _unwrap_if_needed(data: Any, expected_type: type) -> Any:
    """OpenAI json_object mode always returns a dict, never a bare list.
    If we expected a list but got a dict, find the first list value inside it.
    """
    if expected_type is list and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return data


def _validate_schema(data: Any, expected_type: type,
                     required_keys: list[str] | None = None) -> bool:
    """Return True if data matches expected_type and contains required_keys."""
    if not isinstance(data, expected_type):
        return False
    if required_keys:
        sample = data[0] if isinstance(data, list) and data else data
        if isinstance(sample, dict):
            if not all(k in sample for k in required_keys):
                return False
    return True


# ── Core guardrail dispatcher ─────────────────────────────────────────────────

def _llm_call(
    cfg,
    prompt: str,
    fallback: Any,
    agent_name: str,
    provider: str = "anthropic",
    expected_type: type = dict,
    required_keys: list[str] | None = None,
    stage: str = "enrich_stage",
    max_retries: int = 2,
    max_tokens: int = 4096,
) -> Any:
    """
    Dual-provider LLM call with guardrails.

    Guardrail order:
      1. Primary provider, attempt 1
      2. Primary provider, attempt 2 (prompt includes previous error)
      3. Fallback provider, attempt 1
      4. Return `fallback` if all fail
    """
    from langchain_core.messages import HumanMessage
    pg_logger = getattr(cfg, "_pg_logger", None)

    def _try(make_llm_fn, prov_name, model_name, current_prompt, attempt_label):
        t0 = time.time()
        try:
            llm  = make_llm_fn(cfg, max_tokens=max_tokens)
            resp = llm.invoke([HumanMessage(content=current_prompt)])
            inp, out = _token_usage(resp)
            ms = int((time.time() - t0) * 1000)
            if pg_logger:
                pg_logger.log_llm_call(
                    agent_name=f"{agent_name}[{attempt_label}]",
                    stage=stage, provider=prov_name, model=model_name,
                    prompt_tokens=inp, completion_tokens=out,
                    duration_ms=ms, success=True,
                )
            data = _parse_json(resp.content)
            # OpenAI json_object mode always returns a dict — unwrap if we
            # expected a list (e.g. {"rules": [...]}) → [...]
            data = _unwrap_if_needed(data, expected_type)
            if not _validate_schema(data, expected_type, required_keys):
                raise ValueError(
                    f"Schema mismatch: expected {expected_type.__name__} "
                    f"with keys {required_keys}, got {type(data).__name__}"
                )
            return data, None
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            logger.warning("llm_call [%s/%s]: %s", agent_name, attempt_label, exc)
            if pg_logger:
                pg_logger.log_llm_call(
                    agent_name=f"{agent_name}[{attempt_label}]",
                    stage=stage, provider=prov_name, model=model_name,
                    prompt_tokens=0, completion_tokens=0,
                    duration_ms=ms, success=False, error_message=str(exc),
                )
            return None, str(exc)

    alt_provider = "openai" if provider == "anthropic" else "anthropic"
    primary_fn   = _make_anthropic_llm if provider  == "anthropic" else _make_openai_llm
    fallback_fn  = _make_openai_llm   if alt_provider == "openai"  else _make_anthropic_llm
    primary_model  = cfg.anthropic_model if provider     == "anthropic" else cfg.openai_model
    fallback_model = cfg.openai_model    if alt_provider == "openai"    else cfg.anthropic_model

    # Provider-specific JSON instructions
    if provider == "anthropic":
        prompt = prompt + "\n\nIMPORTANT: Reply with valid JSON only. No markdown, no explanation."
    elif provider == "openai" and expected_type is list:
        # json_object mode can't return a bare array — ask for a wrapper key
        prompt = prompt + '\n\nWrap the array in a JSON object: {"items": [...]}'

    current_prompt = prompt
    last_error = ""
    for attempt in range(1, max_retries + 1):
        if last_error:
            current_prompt = (
                f"{prompt}\n\n[Previous attempt failed: {last_error}. "
                "Fix the JSON format and try again.]"
            )
        data, err = _try(primary_fn, provider, primary_model,
                         current_prompt, f"p{attempt}")
        if data is not None:
            return data
        last_error = err or "unknown error"

    # Cross-provider fallback
    fb_prompt = prompt
    if alt_provider == "anthropic":
        fb_prompt = prompt + "\n\nIMPORTANT: Reply with valid JSON only. No markdown, no explanation."
    data, err = _try(fallback_fn, alt_provider, fallback_model, fb_prompt, "fallback")
    if data is not None:
        return data

    logger.error("llm_call [%s]: all attempts failed, returning fallback", agent_name)
    return fallback


# ── 1. StepQuestionRefinerAgent — Anthropic ───────────────────────────────────

def step_question_refiner(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("steps") or []
    if not steps:
        return {}
    raw_questions = [
        {"number": s["number"],
         "raw": (s.get("question") or s.get("raw_text",""))[:300]}
        for s in steps[:20]
    ]
    prompt = f"""You are a healthcare claims policy analyst.
Rewrite each SOP step question to be clear, concise, and in active voice.
Return JSON array only: [{{"number": N, "question": "rewritten question"}}]

Steps:
{json.dumps(raw_questions, indent=2)}"""

    result = _llm_call(cfg, prompt, [], "step_question_refiner",
                       provider="anthropic", expected_type=list,
                       required_keys=["number", "question"])
    if not isinstance(result, list):
        return {}
    q_map = {r["number"]: r.get("question", "") for r in result if "number" in r}
    enriched = [dict(s, question=q_map.get(s["number"], s.get("question", "")))
                for s in steps]
    return {"enriched_steps": enriched}


# ── 2. DecisionRowClassifierAgent — Anthropic ─────────────────────────────────

def decision_row_classifier(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("enriched_steps") or state.get("steps") or []
    all_rows = []
    for s in steps:
        for j, row in enumerate(s.get("decision_rows", [])):
            all_rows.append({
                "step": s["number"], "row": j,
                "if":     row.get("condition_if", ""),
                "action": row.get("action", ""),
                "current": row.get("decision", "CONDITIONAL"),
            })
    if not all_rows:
        return {}

    batch = all_rows[:20]
    prompt = f"""Classify each healthcare claim processing decision rule.

Valid decisions: DENY, ALLOW, BYPASS, PEND, REFER, SYSTEM, STOP, WAIVE, CONDITIONAL

Return JSON array: [{{"step": N, "row": N, "decision": "DENY", "rationale": "brief reason"}}]

Rules to classify:
{json.dumps(batch, indent=2)}"""

    result = _llm_call(cfg, prompt, [], "decision_row_classifier",
                       provider="anthropic", expected_type=list,
                       required_keys=["step", "row", "decision"])
    if not isinstance(result, list):
        return {}
    decision_map = {(r["step"], r["row"]): r["decision"] for r in result if "step" in r}
    enriched = []
    for s in steps:
        s2 = dict(s)
        rows = []
        for j, row in enumerate(s.get("decision_rows", [])):
            r2 = dict(row)
            r2["decision"] = decision_map.get((s["number"], j), row.get("decision", "CONDITIONAL"))
            rows.append(r2)
        s2["decision_rows"] = rows
        enriched.append(s2)
    return {"enriched_steps": enriched}


# ── 3. RuleSemanticEnricherAgent — Anthropic ──────────────────────────────────

def rule_semantic_enricher(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("enriched_steps") or state.get("steps") or []
    sample = []
    for s in steps[:5]:
        for j, row in enumerate(s.get("decision_rows", [])[:4]):
            sample.append({
                "step": s["number"], "row": j,
                "action": row.get("action", ""),
            })
    if not sample:
        return {}

    prompt = f"""For each healthcare claims rule action, extract:
- action_line: line-level override (e.g. "reduce to $0", "bypass duplicate edit")
- action_claim: claim-level outcome (e.g. "deny claim", "allow claim", "pend for review")

Return JSON array: [{{"step": N, "row": N, "action_line": "...", "action_claim": "..."}}]

Rules:
{json.dumps(sample, indent=2)}"""

    result = _llm_call(cfg, prompt, [], "rule_semantic_enricher",
                       provider="anthropic", expected_type=list,
                       required_keys=["step", "row", "action_line", "action_claim"])
    if not isinstance(result, list):
        return {}
    enrich_map = {(r["step"], r["row"]): r for r in result if "step" in r}
    enriched = []
    for s in steps:
        s2 = dict(s)
        rows = []
        for j, row in enumerate(s.get("decision_rows", [])):
            r2 = dict(row)
            extra = enrich_map.get((s["number"], j), {})
            r2["action_line"]  = extra.get("action_line", "")
            r2["action_claim"] = extra.get("action_claim", "")
            rows.append(r2)
        s2["decision_rows"] = rows
        enriched.append(s2)
    return {"enriched_steps": enriched}


# ── 4. CrossReferenceResolverAgent — OpenAI ───────────────────────────────────

def cross_reference_resolver(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("enriched_steps") or state.get("steps") or []
    known = [d.get("title", "") for d in (state.get("all_documents") or []) if d.get("title")]
    refs  = list({r for s in steps for r in s.get("referenced_sops", [])})
    if not refs:
        return {}

    prompt = f"""Match each SOP reference text to the closest known document title.
If no match, set matched to null.

Return JSON array: [{{"ref": "...", "matched": "title or null", "confidence": 0.0}}]

References: {json.dumps(refs[:15])}
Known documents: {json.dumps(known[:30])}"""

    _llm_call(cfg, prompt, [], "cross_reference_resolver",
              provider="openai", expected_type=list, required_keys=["ref"])
    return {}  # enrichment stored in future graph edges, not state


# ── 5. AmbiguousTermResolverAgent — Anthropic ─────────────────────────────────

def ambiguous_term_resolver(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    meta     = state.get("metadata") or {}
    raw_text = (state.get("raw_text") or "")[:3000]
    prompt = f"""Resolve vague terms in this healthcare SOP to their specific meaning.

SOP Title: {meta.get('title', '')}
Platform:  {meta.get('platform', '')}

Excerpt:
{raw_text}

Return JSON object:
{{
  "the_plan":        "specific plan name or type",
  "the_group":       "specific group or entity",
  "member_submitted":"what this means in context",
  "other_terms":     {{"term": "meaning"}}
}}"""

    result = _llm_call(cfg, prompt, {}, "ambiguous_term_resolver",
                       provider="anthropic", expected_type=dict,
                       required_keys=["the_plan"])
    if not isinstance(result, dict):
        return {}
    meta2 = dict(meta)
    meta2["resolved_terms"] = result
    return {"metadata": meta2}


# ── 6. POTFValidatorAgent — Anthropic ────────────────────────────────────────

def potf_validator(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("enriched_steps") or state.get("steps") or []
    step_summary = [
        {"step": s["number"],
         "question": s.get("question", ""),
         "decisions": [r.get("decision") for r in s.get("decision_rows", [])]}
        for s in steps
    ]
    prompt = f"""Review these SOP steps for a healthcare timely filing / duplicate claim policy.

Analyse completeness:
- Are all claim scenarios covered?
- Any missing DENY or ALLOW branches?
- Any gaps or contradictions?

Return JSON:
{{
  "complete": true,
  "gaps": ["description of any gap"],
  "warnings": ["policy concern"],
  "coverage_pct": 95
}}

Steps:
{json.dumps(step_summary[:20], indent=2)}"""

    result = _llm_call(cfg, prompt,
                       {"complete": True, "gaps": [], "warnings": [], "coverage_pct": 100},
                       "potf_validator",
                       provider="anthropic", expected_type=dict,
                       required_keys=["complete", "warnings"])
    if isinstance(result, dict):
        warnings = list(state.get("validation_warnings") or [])
        warnings.extend(result.get("warnings", []))
        return {"validation_warnings": warnings}
    return {}


# ── 7. PreSectionRuleExtractorAgent — OpenAI ─────────────────────────────────

def pre_section_rule_extractor(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Extract structured audit rules from every pre-section block.

    Each rule becomes a structured decision entry (condition → action → type).
    Sections with substantive exception/override logic are flagged with
    is_exception_block=True so the write layer can promote them to the
    decision tree as a "Step 0 — Pre-Step Exceptions" AuditStep.
    """
    pre = state.get("pre_sections") or []
    if not pre:
        return {}

    # Build full-text view of each pre-section — no character truncation on
    # the content itself so we capture every rule in blocks like
    # "Duplicate Exceptions" which can be > 2 000 chars.
    section_texts = []
    for s in pre[:15]:
        raw_text = " ".join(
            (i.get("text", str(i)) if isinstance(i, dict) else str(i))
            for i in s.get("items", [])
        )
        # Limit per-section to 3000 chars to fit in one LLM context
        section_texts.append({
            "name": s.get("name", ""),
            "text": raw_text[:3000],
        })

    prompt = f"""You are reading a healthcare claims SOP as a senior claims auditor.

The sections below appear BEFORE the numbered decision-tree steps. They contain:
• Eligibility/applicability rules (who this SOP covers)
• Exception and override rules (when standard steps do NOT apply)
• Billing-specific rules (e.g. monthly vs per-diem vs 15-minute case services)
• Cross-billing rules, timely-filing rules, code descriptions

For EACH distinct, actionable rule you find:
1. Write it as a standalone IF condition → THEN action pair.
2. Assign decision_type: DENY | ALLOW | BYPASS | OVERRIDE | ELIGIBILITY | REFER | NOTE
3. Flag is_exception=true if the rule says "exclude", "do not apply", "bypass", or overrides normal steps.

Return a JSON array — one object per rule:
[{{
  "section": "<exact section name from input>",
  "condition": "<complete IF condition — be specific, include codes/values>",
  "action": "<complete THEN action — what the auditor must do>",
  "decision_type": "DENY|ALLOW|BYPASS|OVERRIDE|ELIGIBILITY|REFER|NOTE",
  "is_exception": true/false
}}]

Only return the JSON array. No prose. Extract every individual rule — do not combine them.

Sections:
{json.dumps(section_texts, indent=2)}"""

    result = _llm_call(cfg, prompt, [], "pre_section_rule_extractor",
                       provider="openai", expected_type=list,
                       required_keys=["section", "condition", "action"])
    if not isinstance(result, list):
        return {}

    pre2 = [dict(s) for s in pre]
    name_map = {s.get("name", ""): s for s in pre2}

    for rule in result:
        sec_name = rule.get("section", "")
        # Match by exact name first, then substring
        target = name_map.get(sec_name)
        if not target:
            for s in pre2:
                if sec_name in s.get("name", "") or s.get("name", "") in sec_name:
                    target = s
                    break
        if target:
            target.setdefault("llm_rules", []).append(rule)

    # Mark sections that contain exception/override/eligibility rules
    for s in pre2:
        rules = s.get("llm_rules", [])
        s["is_exception_block"] = any(
            r.get("is_exception") or r.get("decision_type") in
            ("DENY", "ALLOW", "BYPASS", "OVERRIDE", "ELIGIBILITY")
            for r in rules
        )

    return {"pre_sections": pre2}


# ── 8. GroupRuleExtractorAgent — OpenAI (was group_rule_nlp) ─────────────────

def group_rule_extractor(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    group_rules = state.get("group_rules") or []
    if not group_rules:
        return {}
    sample = [
        {"group": gr.get("group_name", ""),
         "raw_text": gr.get("raw_text", "")[:400]}
        for gr in group_rules[:15]
    ]
    prompt = f"""Extract precise timely filing limits for each group from this healthcare SOP.

Return JSON array:
[{{
  "group":       "group name",
  "inn_days":    90,
  "oon_days":    180,
  "from":        "DOS or PAID_DATE or EOB_DATE",
  "exceptions":  ["exception description"],
  "notes":       "any special conditions"
}}]

Groups:
{json.dumps(sample, indent=2)}"""

    result = _llm_call(cfg, prompt, [], "group_rule_extractor",
                       provider="openai", expected_type=list,
                       required_keys=["group"])
    if not isinstance(result, list):
        return {}
    result_map = {r["group"]: r for r in result if "group" in r}
    updated = []
    for gr in group_rules:
        gr2 = dict(gr)
        extra = result_map.get(gr.get("group_name", ""), {})
        if extra.get("inn_days") and not gr2.get("limit_days"):
            gr2["limit_days"] = extra["inn_days"]
        if extra.get("inn_days"):
            gr2["inn_days"] = extra["inn_days"]
        if extra.get("oon_days"):
            gr2["oon_days"] = extra["oon_days"]
        if extra.get("from"):
            gr2["calculation_from"] = extra["from"]
        if extra.get("exceptions"):
            gr2["exceptions"] = extra["exceptions"]
        updated.append(gr2)
    return {"group_rules": updated}


# ── 9. DateConditionExtractorAgent — OpenAI (was date_condition_nlp) ──────────

def date_condition_extractor(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    raw = (state.get("raw_text") or "")[:5000]
    if not raw.strip():
        return {}
    prompt = f"""Extract all date-based conditions and effective ranges from this healthcare SOP.

Look for patterns like:
- "claims processed on or after MM/DD/YYYY"
- "DOS between MM/DD/YYYY - MM/DD/YYYY"
- "effective as of MM/DD/YYYY"
- "for dates of service prior to MM/DD/YYYY"

Return JSON array (empty array if none found):
[{{
  "date_from":      "MM/DD/YYYY or null",
  "date_to":        "MM/DD/YYYY or null",
  "effective_date": "MM/DD/YYYY or null",
  "context":        "surrounding sentence",
  "source_field":   "section name or step number"
}}]

Text:
{raw}"""

    result = _llm_call(cfg, prompt, [], "date_condition_extractor",
                       provider="openai", expected_type=list)
    if not isinstance(result, list):
        return {}
    existing = list(state.get("detected_date_conditions") or [])
    existing.extend(result)
    return {"detected_date_conditions": existing}


# ── 10. SummaryGeneratorAgent — Anthropic ────────────────────────────────────

def summary_generator(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    meta  = state.get("metadata") or {}
    steps = state.get("enriched_steps") or state.get("steps") or []
    pre   = state.get("pre_sections") or []
    grps  = state.get("group_rules") or []

    prompt = f"""Write a comprehensive executive summary of this healthcare SOP.

Title:    {meta.get('title', 'Unknown')}
Platform: {meta.get('platform', '')}
LOB:      {meta.get('lob', [])}
Steps:    {len(steps)}
Pre-sections: {[s.get('name','') for s in pre[:5]]}
Group rules:  {[g.get('group_name','') for g in grps[:5]]}
Key decisions:{[s.get('question','') for s in steps[:5]]}

Return JSON:
{{
  "summary":      "3-4 sentence executive summary",
  "purpose":      "one sentence stating what this SOP governs",
  "key_rules":    ["top 3-5 rules as bullet points"],
  "coverage":     "what claims / groups / dates this applies to"
}}"""

    result = _llm_call(cfg, prompt,
                       {"summary": "", "purpose": "", "key_rules": [], "coverage": ""},
                       "summary_generator",
                       provider="anthropic", expected_type=dict,
                       required_keys=["summary"])
    return {"llm_summary": result.get("summary", "") if isinstance(result, dict) else ""}


# ── Backward-compatibility alias (graph.py imports this name) ─────────────────
# Keep old names pointing to new implementations so graph.py needs no change
# for the NLP→extractor rename.
group_rule_nlp       = group_rule_extractor
date_condition_nlp   = date_condition_extractor
