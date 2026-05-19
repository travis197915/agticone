"""Narrative contextualisation for ALREADY-ingested SOPs.

Use case: you have an ``AuditSop`` row sitting in Postgres without
narrative paragraphs (because it was ingested before the narrative
stage existed, or because the LLM call failed during ingestion).
This service rebuilds a minimal pipeline state from the persisted
rows, runs the two narrative agents from ``uhc_sop_ingestion.agents.
a17_narrative``, then writes the results straight back to
``AuditSop.narrative_context`` and ``AuditStep.narrative_context`` —
no Neo4j writes, no re-fetching of the source HTML, no Celery
fan-out beyond this single task.

Exposed surface:
    * ``contextualize_job(job_id)``       — sync function, for shell / tests.
    * ``contextualize_sop(audit_sop_id)`` — sync function for one SOP.
    * tasks.run_narrative_contextualizer  — Celery wrapper (see ``tasks.py``).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction

from ..models import AuditSop, AuditStep, IngestionJob

log = logging.getLogger(__name__)

_ENV_PATH = Path(settings.BASE_DIR) / ".env"


# ── State rebuild ─────────────────────────────────────────────────────────────

def _rebuild_state(sop: AuditSop) -> dict[str, Any]:
    """Reconstitute the minimal slice of PipelineState the narrative agents need.

    The agents only read: ``metadata``, ``raw_text``, ``pre_sections``,
    ``steps`` (with ``step_number``, ``question``, ``intro_text``,
    ``decision_rows``).  Everything else can stay empty.
    """
    pre = [{
        "name":       p.label,
        "category":   p.category,
        "content":    p.content_text,
        "llm_rules":  p.llm_rules or [],
        "is_blocking": p.is_blocking,
    } for p in sop.preconditions.all().order_by("display_order", "id")]

    steps: list[dict[str, Any]] = []
    for s in sop.steps.prefetch_related("decisions").order_by("step_number"):
        steps.append({
            "step_number":     s.step_number,
            "number":          s.step_number,
            "question":        s.question,
            "intro_text":      s.intro_text,
            "is_terminal":     s.is_terminal,
            "terminal_action": s.terminal_action,
            "decision_rows": [{
                "condition_if":   d.condition_if,
                "condition_and":  d.condition_and,
                "action":         d.action_text,
                "action_summary": d.action_summary,
                "decision":       d.decision_type,
                "decision_type":  d.decision_type,
                "goto_step":      d.goto_step,
                "is_final":       d.is_final,
                "eob_codes":      d.eob_codes or [],
                "ex_codes":       d.ex_codes or [],
                "denial_codes":   d.denial_codes or [],
                "system_actions": d.system_actions or [],
                "codes":          d.all_codes or [],
            } for d in s.decisions.all().order_by("row_index")],
        })

    return {
        "raw_text": sop.raw_text or "",
        "metadata": {
            "title":          sop.title or "",
            "platform":       sop.platform or "",
            "lob":            sop.lob or [],
            "audience":       sop.audience or [],
            "purpose":        sop.purpose or "",
            "effective_date": sop.effective_date,
            "revision_date":  sop.revision_date,
        },
        "pre_sections":   pre,
        "steps":          steps,
        "enriched_steps": steps,
    }


# ── Core entry point ─────────────────────────────────────────────────────────

def contextualize_sop(sop: AuditSop) -> dict[str, Any]:
    """Run both narrative agents on one ``AuditSop`` and persist results.

    Returns a small dict summarising what changed, mainly for logging /
    Celery return value.
    """
    # Lazy import — the package may not be importable in every environment
    # (e.g. test containers without LangChain installed).
    from uhc_sop_ingestion.config import PipelineConfig
    from uhc_sop_ingestion.agents.a17_narrative import (
        sop_overview_narrator, step_narrative_writer,
    )

    cfg = PipelineConfig.from_env(env_path=_ENV_PATH if _ENV_PATH.exists() else None)
    state = _rebuild_state(sop)

    # Run agents — each returns a delta that we merge into state.
    delta1 = sop_overview_narrator(state, cfg) or {}
    state.update(delta1)
    delta2 = step_narrative_writer(state, cfg) or {}
    state.update(delta2)

    sop_narrative = (state.get("sop_narrative") or "").strip()
    narrated_steps = {
        int(s["step_number"]): (s.get("narrative_context") or "").strip()
        for s in (state.get("steps") or [])
        if s.get("step_number") is not None and s.get("narrative_context")
    }

    with transaction.atomic():
        if sop_narrative:
            sop.narrative_context = sop_narrative
            sop.save(update_fields=["narrative_context", "updated_at"])
        if narrated_steps:
            for step in sop.steps.filter(step_number__in=narrated_steps.keys()):
                text = narrated_steps.get(step.step_number)
                if not text:
                    continue
                step.narrative_context = text
                step.save(update_fields=["narrative_context"])

    log.info(
        "contextualize_sop sop=%s sop_narrative=%d chars steps_narrated=%d/%d",
        sop.id, len(sop_narrative), len(narrated_steps), sop.steps.count(),
    )

    return {
        "audit_sop_id":     sop.id,
        "sop_narrative":    bool(sop_narrative),
        "steps_narrated":   len(narrated_steps),
        "steps_total":      sop.steps.count(),
    }


def contextualize_job(job_id: str) -> list[dict[str, Any]]:
    """Run ``contextualize_sop`` for every ``AuditSop`` linked to a job."""
    try:
        job = IngestionJob.objects.get(pk=job_id)
    except IngestionJob.DoesNotExist:
        log.warning("contextualize_job: job %s not found", job_id)
        return []

    results: list[dict[str, Any]] = []
    for sop in job.audit_sops.all().order_by("id"):
        try:
            results.append(contextualize_sop(sop))
        except Exception:  # pragma: no cover — defensive
            log.exception("contextualize_sop failed for sop=%s", sop.id)
            results.append({"audit_sop_id": sop.id, "error": True})
    return results
