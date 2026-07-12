"""Executive-summary add-on.

Condenses one claim run — every agent/shape, its rolled-up status, and the
final verdict — into a short blob a human auditor can read in seconds:

* ``headline``        — one-sentence verdict headline
* ``overall_summary`` — 2-4 sentence executive narrative
* ``key_findings``    — up to five bullet points worth noticing
* ``step_summaries``  — one plain-language line per agent/step

The heavy lifting is one LLM call (Anthropic primary, OpenAI fallback via the
engine's guarded ``llm_call``). If the LLM is unavailable or ``NO_LLM=1`` is
set, a deterministic fallback still produces a usable summary from the engine's
own reasoning strings, so the row is always written.

Reads exclusively from the persisted run/evaluation rows, so the same code path
serves both the inline engine agent (n08, post-persist) and the offline
``backfill_executive_summary`` command.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Cap how many steps we send to the model / persist inline reasoning for, so a
# pathologically large workflow can't blow the prompt budget.
_MAX_STEPS = 130
_MAX_REASONINGS_PER_STEP = 6
# Output budget for the summary call. A claim can have ~100 steps and we ask for
# one line each, so a small budget truncates the JSON mid-string and the parse
# fails. Sized to comfortably cover ~130 short lines + the overall narrative.
_SUMMARY_MAX_TOKENS = 8192


def _no_llm() -> bool:
    return os.environ.get("NO_LLM", "").strip() in ("1", "true", "True", "yes")


def _collect_steps(run) -> list[dict[str, Any]]:
    """Group the run's evaluations into per-agent/shape steps with status.

    Reuses the same DB rollup the summary endpoint uses so the executive
    summary lines up 1:1 with what the auditor sees on the agents tab.
    """
    from .views import _agent_status_light, _build_summary_rollup

    nodes, _outer = _build_summary_rollup(run)
    steps: list[dict[str, Any]] = []
    for node in nodes:
        reasonings = [r for r in node.get("reasonings", []) if r][
            :_MAX_REASONINGS_PER_STEP
        ]
        steps.append({
            "shape_id": node["shape_id"],
            "agent_name": node["shape_label"] or node["shape_id"],
            "status": _agent_status_light(node),
            "decisions": list(node.get("matched_decisions") or []),
            "reasonings": reasonings,
        })
    return steps


def _audit_status(run, steps: list[dict[str, Any]]) -> str:
    from . import trace_builder

    status = trace_builder.normalize_decision(run.final_decision_type or "")
    if status:
        return status
    return trace_builder.aggregate_status([s["status"] for s in steps])


def _lob_label(run) -> str:
    return (run.claim_lob or {}).get("label", "") if isinstance(run.claim_lob, dict) else ""


def _build_prompt(run, steps: list[dict[str, Any]], audit_status: str) -> str:
    import json

    compact = {
        "claim_id": run.claim_id or "",
        "line_of_business": _lob_label(run),
        "final_verdict": run.final_decision_type or "",
        "audit_status": audit_status,
        "applied_codes": list(run.applied_codes or []),
        "engine_narrative": (run.narrative or "")[:2000],
        "steps": [
            {
                "shape_id": s["shape_id"],
                "agent": s["agent_name"],
                "status": s["status"],
                "decisions": s["decisions"],
                "notes": " ".join(s["reasonings"])[:900],
            }
            for s in steps[:_MAX_STEPS]
        ],
    }
    return (
        "You are writing an executive audit summary for a human claims auditor.\n"
        "Below is the machine audit of a single healthcare claim: the final "
        "verdict, the rolled-up audit status, and every agent/step that ran with "
        "its own status and reasoning.\n\n"
        "Write a TIGHT, plain-English summary. Do not restate every rule; "
        "synthesize. A busy auditor should understand the claim's outcome and "
        "why in under 20 seconds.\n\n"
        "Return ONLY JSON with this exact shape:\n"
        "{\n"
        '  "headline": "<=15 word verdict headline",\n'
        '  "overall_summary": "2-4 sentence executive narrative of the whole claim",\n'
        '  "key_findings": ["<=5 short bullets a human should notice"],\n'
        '  "steps": [{"shape_id": "<echo the shape_id>", "summary": "one plain sentence for this step"}]\n'
        "}\n\n"
        "Rules: include one steps[] entry per input step, echoing its shape_id. "
        "Keep each step summary to a single sentence in auditor language "
        "(e.g. 'Coverage validated — member eligible, no defect'). If the claim "
        "is clean, say so plainly; if there is a defect, lead with it.\n\n"
        f"AUDIT DATA:\n{json.dumps(compact, ensure_ascii=False)}"
    )


def _fallback(run, steps: list[dict[str, Any]], audit_status: str) -> dict[str, Any]:
    """Deterministic summary when no LLM is available."""
    verdict = run.final_decision_type or "UNKNOWN"
    n = len(steps)
    defects = [s for s in steps if s["status"] == "DEFECT"]
    if audit_status == "CLEAN":
        headline = f"Claim {run.claim_id or ''} is clean — no defect ({verdict})."
        overall = (
            f"All {n} agent step(s) were evaluated and none applied an adverse "
            f"disposition. Final engine verdict is {verdict} (audit status CLEAN)."
        )
    elif audit_status == "DEFECT":
        first = defects[0]["agent_name"] if defects else "a step"
        headline = f"Claim {run.claim_id or ''} has a defect ({verdict})."
        overall = (
            f"{len(defects)} of {n} agent step(s) flagged a defect (first at "
            f"'{first}'). Final engine verdict is {verdict}."
        )
    else:
        headline = f"Claim {run.claim_id or ''} is inconclusive ({verdict})."
        overall = (
            f"The audit could not be fully concluded across {n} step(s). "
            f"Final engine verdict is {verdict} (audit status {audit_status})."
        )
    if run.narrative:
        overall = f"{overall} {run.narrative.strip()[:400]}"
    key_findings: list[str] = []
    if run.applied_codes:
        key_findings.append("Applied codes: " + ", ".join(map(str, run.applied_codes)))
    for s in defects[:4]:
        note = s["reasonings"][0] if s["reasonings"] else ""
        key_findings.append(f"{s['agent_name']}: DEFECT. {note}".strip()[:200])
    step_out = []
    for s in steps:
        note = s["reasonings"][0] if s["reasonings"] else ""
        step_out.append({
            "shape_id": s["shape_id"],
            "summary": f"{s['status']} — {note}".strip(" —")[:280] or s["status"],
        })
    return {
        "headline": headline[:512],
        "overall_summary": overall,
        "key_findings": key_findings[:5],
        "steps": step_out,
    }


def _call_llm(run, steps: list[dict[str, Any]], audit_status: str
              ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return (parsed, meta) or (None, {}) on failure."""
    try:
        from uhc_execution_engine.config import get_config
        from uhc_execution_engine.llm import execution_run_context, llm_call
    except Exception:  # pragma: no cover - engine must be importable in prod
        logger.warning("executive_summary: engine LLM helpers unavailable")
        return None, {}

    prompt = _build_prompt(run, steps, audit_status)
    fallback = _fallback(run, steps, audit_status)
    try:
        with execution_run_context(str(run.id)):
            data, meta = llm_call(
                get_config(),
                prompt,
                agent_name="executive_summary",
                stage="executive_summary",
                fallback=fallback,
                expected_type=dict,
                required_keys=["overall_summary"],
                max_tokens=_SUMMARY_MAX_TOKENS,
            )
        return data, meta
    except Exception:  # pragma: no cover - never break the run
        logger.exception("executive_summary: llm_call failed run=%s", run.id)
        return None, {}


def _merge_steps(steps: list[dict[str, Any]],
                 llm_steps: Any) -> list[dict[str, Any]]:
    """Attach the model's one-liners back onto the real step metadata."""
    by_id: dict[str, str] = {}
    if isinstance(llm_steps, list):
        for item in llm_steps:
            if isinstance(item, dict) and item.get("shape_id"):
                by_id[str(item["shape_id"])] = str(item.get("summary") or "").strip()
    out: list[dict[str, Any]] = []
    for s in steps:
        summary = by_id.get(s["shape_id"], "")
        if not summary:
            note = s["reasonings"][0] if s["reasonings"] else ""
            summary = f"{s['status']} — {note}".strip(" —") or s["status"]
        out.append({
            "shape_id": s["shape_id"],
            "agent_name": s["agent_name"],
            "status": s["status"],
            "summary": summary[:400],
        })
    return out


def generate_for_run(run, *, source: str = "agent",
                     force: bool = False):
    """Generate + persist a ClaimExecutiveSummary for ``run``.

    Idempotent (OneToOne upsert). Returns the row, or ``None`` when there is
    nothing to summarize (e.g. a failed run with no evaluations). Best-effort:
    callers in the engine swallow exceptions so this can never break a run.
    """
    from .models import ClaimExecutiveSummary

    if not force:
        existing = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
        if existing is not None:
            return existing

    steps = _collect_steps(run)
    if not steps and not (run.narrative or run.final_decision_type):
        logger.info("executive_summary: nothing to summarize for run=%s", run.id)
        return None

    audit_status = _audit_status(run, steps)

    data: dict[str, Any] | None = None
    meta: dict[str, Any] = {}
    used_source = source
    if not _no_llm():
        data, meta = _call_llm(run, steps, audit_status)
    # A real LLM success stamps meta["provider"]; if it is empty the guarded
    # call exhausted every attempt and handed back our fallback dict, so label
    # the row honestly as a fallback rather than an LLM-authored summary.
    llm_ok = (
        isinstance(data, dict)
        and bool(data.get("overall_summary"))
        and bool(meta.get("provider"))
    )
    if not llm_ok:
        data = _fallback(run, steps, audit_status)
        used_source = "fallback"

    step_summaries = _merge_steps(steps, data.get("steps"))
    key_findings = [
        str(k).strip()[:280]
        for k in (data.get("key_findings") or [])
        if str(k).strip()
    ][:5]

    row, _created = ClaimExecutiveSummary.objects.update_or_create(
        run_id=run.id,
        defaults=dict(
            claim_id=run.claim_id or "",
            verdict=run.final_decision_type or "",
            audit_status=audit_status,
            headline=str(data.get("headline") or "")[:512],
            overall_summary=str(data.get("overall_summary") or ""),
            key_findings=key_findings,
            step_summaries=step_summaries,
            llm_provider=str(meta.get("provider") or ""),
            llm_model=str(meta.get("model") or ""),
            generated_by=used_source,
        ),
    )
    logger.info(
        "executive_summary: wrote run=%s claim=%s status=%s steps=%d src=%s",
        run.id, run.claim_id or "-", audit_status, len(step_summaries), used_source,
    )
    return row
