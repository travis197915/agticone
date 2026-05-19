"""CONTEXT EXTRACTION LAYER — 12 agents (LLM-powered, zero regex).

All code detection is done by a single OpenAI JSON-mode call in
`eob_code_detector` (first agent in the chain).  Subsequent agents return {}
to avoid redundant calls — they exist only to preserve the graph structure.
`code_deduplicator` post-processes the result list in pure Python.

Code types extracted by the LLM:
  EOB        — E/F/W + 2-digit EOB codes (E01, F09, W45)
  EX         — Exception codes (OCA, o01, 020, 003)
  DENIAL     — Denial codes (346, CDD, etc.)
  POS        — Place-of-service codes
  REVENUE    — Revenue codes (3-4 digit)
  BILL_TYPE  — Type-of-bill codes (3 digit)
  MODIFIER   — Procedure modifiers (GT, 95, etc.)
  FREQUENCY  — Frequency digits (7, 8)
  SYSTEM_ACT — System actions (F3, F4, F5)
  CPT        — CPT / HCPCS procedure codes
  UNKNOWN    — Other codes that look clinically significant
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)

_EXTRACTED_FLAG = "_ctx_codes_extracted"


def _all_text(state) -> str:
    """Collect all structured text into one block for the LLM."""
    parts = []
    for ps in (state.get("pre_sections") or []):
        for item in ps.get("items", []):
            if item.get("text"):
                parts.append(f"[pre:{ps.get('name','')}] {item['text']}")
    for s in (state.get("enriched_steps") or state.get("steps") or []):
        n = s["number"]
        if s.get("question"):
            parts.append(f"[step:{n}:q] {s['question']}")
        for j, row in enumerate(s.get("decision_rows", [])):
            for k in ("condition_if", "condition_and", "action"):
                if row.get(k):
                    parts.append(f"[step:{n}:row:{j}:{k}] {row[k]}")
    raw = (state.get("raw_text") or "")[:4000]
    if raw:
        parts.append(f"[raw] {raw}")
    return "\n".join(parts)[:6000]


def _llm_extract_codes(cfg, text: str) -> list[dict]:
    """Single OpenAI JSON-mode call to extract all code types."""
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage

    pg_logger = getattr(cfg, "_pg_logger", None)
    t0 = time.time()

    prompt = f"""You are a healthcare claims coding expert.

Extract EVERY medical or claims processing code from the text below.
Include only codes that are explicitly mentioned.

Code types:
- EOB: Explanation-of-benefit codes starting with E, F, or W followed by 2 digits (E01, F09, W45)
- EX: Exception codes like OCA, o01, 020, 003
- DENIAL: Denial codes like 346, CDD
- POS: Place-of-service numeric codes
- REVENUE: Revenue codes (3-4 digit numbers in revenue context)
- BILL_TYPE: Type-of-bill codes (3 digit)
- MODIFIER: Procedure modifiers (GT, 95, etc.)
- FREQUENCY: Frequency codes (7, 8)
- SYSTEM_ACT: System action keys (F3, F4, F5)
- CPT: CPT or HCPCS procedure codes

Return JSON (empty array if no codes found):
[{{
  "raw_value":       "E01",
  "code_system":     "EOB",
  "context_snippet": "surrounding 10-15 words",
  "source_field":    "step:3:row:1:action or pre:Background or raw",
  "confidence":      0.95
}}]

Text to analyse:
{text}"""

    try:
        llm = ChatOpenAI(
            model=cfg.openai_model,
            api_key=cfg.openai_api_key,
            max_tokens=4096,
            model_kwargs={"response_format": {"type": "json_object"}},
        )
        # Wrap in object because json_object mode requires an object root
        wrapped_prompt = (
            prompt.replace(
                "Return JSON (empty array if no codes found):\n[{",
                'Return JSON object: {"codes": [{'
            ).replace(
                "}]\n\nText",
                "}]}\n\nText"
            )
        )
        resp  = llm.invoke([HumanMessage(content=wrapped_prompt)])
        usage = getattr(resp, "usage_metadata", None) or {}
        inp   = usage.get("input_tokens", 0)
        out   = usage.get("output_tokens", 0)
        ms    = int((time.time() - t0) * 1000)

        if pg_logger:
            pg_logger.log_llm_call(
                agent_name="code_detector",
                stage="context_stage",
                provider="openai",
                model=cfg.openai_model,
                prompt_tokens=inp,
                completion_tokens=out,
                duration_ms=ms,
                success=True,
            )

        data = json.loads(resp.content)
        # OpenAI json_object mode always wraps in a dict — find the list
        if isinstance(data, dict):
            codes = data.get("codes") or next(
                (v for v in data.values() if isinstance(v, list)), []
            )
        else:
            codes = data if isinstance(data, list) else []
        return codes

    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        logger.warning("code_detector LLM error: %s", exc)
        if pg_logger:
            pg_logger.log_llm_call(
                agent_name="code_detector",
                stage="context_stage",
                provider="openai",
                model=getattr(cfg, "openai_model", "unknown"),
                prompt_tokens=0,
                completion_tokens=0,
                duration_ms=ms,
                success=False,
                error_message=str(exc),
            )
        return []


def _llm_extract_list_refs(cfg, text: str) -> list[dict]:
    """OpenAI call to detect entity list references."""
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage

    t0 = time.time()
    pg_logger = getattr(cfg, "_pg_logger", None)
    prompt = f"""Identify references to external lists, tables, or reference documents in this SOP text.

Examples: "following list of TINs", "refer to the prevailing code list", "see the facility table below"

Return JSON object:
{{"refs": [{{
  "raw_text":   "exact quoted phrase",
  "list_type":  "TIN_LIST|FACILITY_LIST|CODE_TABLE|REFERENCE_LIST|UNKNOWN_LIST",
  "source_field": "section or step context",
  "is_resolved": false
}}]}}

Text:
{text[:3000]}"""

    try:
        llm = ChatOpenAI(
            model=cfg.openai_model,
            api_key=cfg.openai_api_key,
            max_tokens=2048,
            model_kwargs={"response_format": {"type": "json_object"}},
        )
        resp  = llm.invoke([HumanMessage(content=prompt)])
        usage = getattr(resp, "usage_metadata", None) or {}
        ms    = int((time.time() - t0) * 1000)
        if pg_logger:
            pg_logger.log_llm_call(
                agent_name="entity_list_ref_detector",
                stage="context_stage",
                provider="openai",
                model=cfg.openai_model,
                prompt_tokens=usage.get("input_tokens", 0),
                completion_tokens=usage.get("output_tokens", 0),
                duration_ms=ms,
                success=True,
            )
        data = json.loads(resp.content)
        if isinstance(data, dict):
            refs = data.get("refs") or next(
                (v for v in data.values() if isinstance(v, list)), []
            )
        else:
            refs = data if isinstance(data, list) else []
        return refs
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        logger.warning("entity_list_ref_detector error: %s", exc)
        if pg_logger:
            pg_logger.log_llm_call(
                agent_name="entity_list_ref_detector",
                stage="context_stage",
                provider="openai",
                model=getattr(cfg, "openai_model", "unknown"),
                prompt_tokens=0, completion_tokens=0,
                duration_ms=ms, success=False, error_message=str(exc),
            )
        return []


# ── 1. EOBCodeDetectorAgent — runs the full LLM extraction ───────────────────

def eob_code_detector(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    text   = _all_text(state)
    codes  = list(state.get("detected_codes") or [])
    new    = _llm_extract_codes(cfg, text)
    codes.extend(new)
    # Also pick up CPT entries from XLSX code tables (pass-through, no LLM needed)
    for entry in (state.get("code_table_entries") or []):
        codes.append({
            "raw_value":       entry.get("code", ""),
            "code_system":     entry.get("code_system", "CPT"),
            "context_snippet": entry.get("description", "")[:100],
            "source_field":    f"xlsx:{entry.get('source_sheet', '')}",
            "confidence":      1.0,
        })
    return {"detected_codes": codes, _EXTRACTED_FLAG: True}


# ── 2-10. Remaining agents — no-ops (extraction already done above) ───────────

def ex_code_detector(state, cfg):         return {}
def denial_code_detector(state, cfg):     return {}
def pos_code_detector(state, cfg):        return {}
def revenue_code_detector(state, cfg):    return {}
def bill_type_detector(state, cfg):       return {}
def modifier_code_detector(state, cfg):   return {}
def frequency_code_detector(state, cfg):  return {}
def system_action_detector(state, cfg):   return {}
def cpt_code_detector(state, cfg):        return {}


# ── 11. EntityListRefDetectorAgent — LLM ────────────────────────────────────

def entity_list_ref_detector(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    text = _all_text(state)
    refs = list(state.get("detected_list_refs") or [])
    new  = _llm_extract_list_refs(cfg, text)
    refs.extend(new)
    return {"detected_list_refs": refs}


# ── 12. CodeDeduplicatorAgent — pure Python (no LLM needed) ─────────────────

def code_deduplicator(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    codes = state.get("detected_codes") or []
    best: dict[tuple, dict] = {}
    for c in codes:
        if not c.get("raw_value"):
            continue
        key = (c["raw_value"].strip(), c.get("code_system", "UNKNOWN"))
        if key not in best or c.get("confidence", 0) > best[key].get("confidence", 0):
            best[key] = c
    return {"detected_codes": list(best.values())}
