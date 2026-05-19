"""VALIDATION LAYER — 6 agents.

1. DocumentCompletenessAgent — title + steps + at least 1 rule
2. StepSequenceAgent         — steps are sequential without gaps
3. DecisionRowAgent          — every step with a question has ≥1 decision row
4. CodeSystemAgent           — all detected_codes have a known code_system
5. LinkValidatorAgent        — no link points back to itself (cycle guard)
6. MetadataValidatorAgent    — required metadata fields are populated
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)

_KNOWN_CODE_SYSTEMS = {"EOB","EX","DENIAL","POS","REVENUE","BILL_TYPE",
                        "MODIFIER","FREQUENCY","SYSTEM_ACT","CPT","TERM","UNKNOWN"}


# ── 1. DocumentCompletenessAgent ─────────────────────────────────────────────

def document_completeness(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    warns = []
    if not (state.get("metadata") or {}).get("title"):
        warns.append("validation: no title extracted")
    if not state.get("steps"):
        warns.append("validation: no steps extracted")
    if not state.get("pre_sections") and not state.get("steps"):
        warns.append("validation: document appears empty")
    passed = len(warns) == 0
    return {"validation_passed": passed, "validation_warnings": warns}


# ── 2. StepSequenceAgent ─────────────────────────────────────────────────────

def step_sequence(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("enriched_steps") or state.get("steps") or []
    nums = sorted(s["number"] for s in steps)
    warns = []
    for i in range(len(nums)-1):
        if nums[i+1] - nums[i] > 1:
            warns.append(f"validation: gap in step sequence between {nums[i]} and {nums[i+1]}")
    if not nums:
        warns.append("validation: no numbered steps found")
    return {"validation_warnings": warns}


# ── 3. DecisionRowAgent ───────────────────────────────────────────────────────

def decision_row_check(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    steps = state.get("enriched_steps") or state.get("steps") or []
    warns = []
    for s in steps:
        if s.get("question") and not s.get("branch_yes") and not s.get("decision_rows"):
            warns.append(f"validation: step {s['number']} has question but no decision rows")
    return {"validation_warnings": warns}


# ── 4. CodeSystemAgent ────────────────────────────────────────────────────────

def code_system_check(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    codes = state.get("detected_codes") or []
    warns = []
    for c in codes:
        if c.get("code_system") not in _KNOWN_CODE_SYSTEMS:
            warns.append(f"validation: unknown code_system '{c.get('code_system')}' for {c.get('raw_value')}")
    return {"validation_warnings": warns}


# ── 5. LinkValidatorAgent ─────────────────────────────────────────────────────

def link_validator(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    current = state.get("current_url","")
    links   = state.get("links") or []
    warns   = []
    for link in links:
        if link.get("resolved_url") == current:
            warns.append(f"validation: self-referential link detected: {current}")
    return {"validation_warnings": warns}


# ── 6. MetadataValidatorAgent ─────────────────────────────────────────────────

def metadata_validator(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    meta  = state.get("metadata") or {}
    warns = []
    for field in ("effective_date", "platform"):
        if not meta.get(field):
            warns.append(f"validation: missing metadata field '{field}'")
    return {"validation_warnings": warns}
