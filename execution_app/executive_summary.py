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
# The summary is generated in SMALL CHUNKS rather than one giant call. A single
# ~100-step call asking for one line each needs a huge output budget (8k+
# tokens), and that one slow generation trips the gateway read timeout → the
# whole summary falls back. Instead:
#   * one small "overall" call  → headline + narrative + key_findings
#   * N "step" calls of _STEP_CHUNK_SIZE steps each → the per-step one-liners
# Each call has a small output budget so it returns quickly (like the fast
# per-rule claim-processing calls) and a slow/failed chunk only degrades its own
# steps, never the whole summary.
_STEP_CHUNK_SIZE = 20
_OVERALL_MAX_TOKENS = 1200
_CHUNK_MAX_TOKENS = 2500


def _no_llm() -> bool:
    return os.environ.get("NO_LLM", "").strip() in ("1", "true", "True", "yes")


def _collect_steps(run) -> list[dict[str, Any]]:
    """Group the run's evaluations into per-agent/shape steps with status.

    Reuses the same DB rollup the summary endpoint uses so the executive
    summary lines up 1:1 with what the auditor sees on the agents tab.
    """
    from .views import _agent_status_light, _build_summary_rollup
    from . import trace_builder

    nodes, _outer = _build_summary_rollup(run)
    steps: list[dict[str, Any]] = []
    for node in nodes:
        status = _agent_status_light(node)
        # A step the auditor marked NOT APPLICABLE is a non-scoring gate — it never
        # produced a finding and must never surface in the executive summary
        # (headline / narrative / key findings / per-step line). Drop it entirely
        # so its reasoning can't be synthesized into a bullet or sentence.
        if status == trace_builder.NOT_APPLICABLE:
            continue
        # Even on a mixed node (some rules evaluated, some marked NOT APPLICABLE),
        # drop the NA rules' own reasoning lines — a skip reason ("not-applicable:
        # …") is never a finding and must not seed a summary bullet/sentence.
        reasonings = [
            r for r in node.get("reasonings", [])
            if r and not r.lstrip().lower().startswith("not-applicable")
        ][:_MAX_REASONINGS_PER_STEP]
        steps.append({
            "shape_id": node["shape_id"],
            "agent_name": node["shape_label"] or node["shape_id"],
            "status": status,
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


def _compact_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "shape_id": s["shape_id"],
            "agent": s["agent_name"],
            "status": s["status"],
            "decisions": s["decisions"],
            "notes": " ".join(s["reasonings"])[:600],
        }
        for s in steps
    ]


def _build_overall_prompt(run, steps: list[dict[str, Any]], audit_status: str) -> str:
    """Small-OUTPUT call: the whole-claim headline + narrative + key findings.

    The step list is sent as INPUT context (cheap, fast) but the model is asked
    for ONLY the executive fields — no per-step lines — so the output is small
    and the call returns quickly.
    """
    import json

    compact = {
        "claim_id": run.claim_id or "",
        "line_of_business": _lob_label(run),
        "final_verdict": run.final_decision_type or "",
        "audit_status": audit_status,
        "applied_codes": list(run.applied_codes or []),
        "engine_narrative": (run.narrative or "")[:2000],
        "steps": _compact_steps(steps[:_MAX_STEPS]),
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
        '  "key_findings": ["<=5 short bullets a human should notice"]\n'
        "}\n\n"
        "If the claim is clean, say so plainly; if there is a defect, lead with "
        "it.\n\n"
        f"AUDIT DATA:\n{json.dumps(compact, ensure_ascii=False)}"
    )


def _build_chunk_prompt(run, chunk: list[dict[str, Any]]) -> str:
    """One call for a SMALL batch of steps → a one-line summary per step."""
    import json

    compact = {
        "claim_id": run.claim_id or "",
        "final_verdict": run.final_decision_type or "",
        "steps": _compact_steps(chunk),
    }
    return (
        "You are labeling individual audit steps for one healthcare claim. For "
        "EACH step below, write one plain-English sentence in auditor language "
        "(e.g. 'Coverage validated — member eligible, no defect').\n\n"
        "Return ONLY JSON with this exact shape:\n"
        '{ "steps": [{"shape_id": "<echo the shape_id>", '
        '"summary": "one sentence for this step"}] }\n\n'
        "Include exactly one entry per input step, echoing its shape_id.\n\n"
        f"STEPS:\n{json.dumps(compact, ensure_ascii=False)}"
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


def _guarded_call(run, prompt: str, *, stage: str, required_keys: list[str],
                  max_tokens: int, fallback: dict[str, Any]
                  ) -> tuple[dict[str, Any], dict[str, Any]]:
    """One guarded llm_call in the engine's run context.

    Returns ``(data, meta)``. ``meta['provider']`` is set only on a genuine LLM
    success; on any failure the engine's ``llm_call`` hands back ``fallback``
    with an empty provider, so callers can detect fallback via ``meta``.
    """
    try:
        from uhc_execution_engine.config import get_config
        from uhc_execution_engine.llm import execution_run_context, llm_call
    except Exception:  # pragma: no cover - engine must be importable in prod
        logger.warning("executive_summary: engine LLM helpers unavailable")
        return fallback, {}
    try:
        with execution_run_context(str(run.id)):
            return llm_call(
                get_config(), prompt,
                agent_name="executive_summary", stage=stage,
                fallback=fallback, expected_type=dict,
                required_keys=required_keys, max_tokens=max_tokens,
            )
    except Exception:  # pragma: no cover - never break the run
        logger.exception("executive_summary: llm_call failed run=%s stage=%s",
                         run.id, stage)
        return fallback, {}


def _chunks(seq: list[Any], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _call_chunked(run, steps: list[dict[str, Any]], audit_status: str
                  ) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Generate the summary in small chunks (fast, timeout-safe).

    One small "overall" call for headline/narrative/findings, then N per-step
    chunk calls. Returns ``(data, meta, llm_ok)`` where ``llm_ok`` is True only
    when the overall call was genuinely LLM-authored. A slow/failed step chunk
    degrades only its own steps to the deterministic line.
    """
    fb = _fallback(run, steps, audit_status)

    overall, ometa = _guarded_call(
        run, _build_overall_prompt(run, steps, audit_status),
        stage="executive_summary",
        required_keys=["overall_summary"],
        max_tokens=_OVERALL_MAX_TOKENS,
        fallback={k: fb[k] for k in ("headline", "overall_summary", "key_findings")},
    )
    llm_ok = bool(
        isinstance(overall, dict)
        and overall.get("overall_summary")
        and ometa.get("provider")
    )

    fb_lines = {s["shape_id"]: s["summary"] for s in fb["steps"]}
    line_by_id: dict[str, str] = {}
    for chunk in _chunks(steps, _STEP_CHUNK_SIZE):
        cdata, _cmeta = _guarded_call(
            run, _build_chunk_prompt(run, chunk),
            stage="executive_summary_steps",
            required_keys=["steps"],
            max_tokens=_CHUNK_MAX_TOKENS,
            fallback={"steps": [
                {"shape_id": s["shape_id"], "summary": fb_lines.get(s["shape_id"], "")}
                for s in chunk
            ]},
        )
        for item in (cdata.get("steps") if isinstance(cdata, dict) else []) or []:
            if isinstance(item, dict) and item.get("shape_id"):
                line_by_id[str(item["shape_id"])] = str(item.get("summary") or "").strip()

    data = {
        "headline": (overall.get("headline") if isinstance(overall, dict) else "") or "",
        "overall_summary": (overall.get("overall_summary") if isinstance(overall, dict) else "") or "",
        "key_findings": (overall.get("key_findings") if isinstance(overall, dict) else []) or [],
        "steps": [{"shape_id": sid, "summary": summ}
                  for sid, summ in line_by_id.items() if summ],
    }
    return data, ometa, llm_ok


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

    data: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    used_source = source
    llm_ok = False
    if not _no_llm():
        # Chunked generation: small "overall" call + per-step chunks, each with a
        # small output budget so no single call is slow enough to trip the
        # gateway timeout. ``llm_ok`` reflects the overall (executive) call only.
        data, meta, llm_ok = _call_chunked(run, steps, audit_status)
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
