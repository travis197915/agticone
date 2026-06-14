"""Run one SOP ingestion job in a standalone process (subprocess slave).

Called from ``sop_ingestion/worker/job_runner.py`` — not from the Celery master.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from django.core.exceptions import ObjectDoesNotExist

log = logging.getLogger(__name__)

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def execute_ingestion_job(job_id: str) -> dict:
    """Load job, run LangGraph pipeline, persist results. Returns status dict."""
    from .models import IngestionJob, IngestedDocument

    try:
        from uhc_sop_ingestion.pipeline import SopIngestionPipeline
    except ImportError as exc:
        log.error("uhc-sop-ingestion not installed: %s", exc)
        try:
            IngestionJob.objects.get(pk=job_id).mark_failed(f"Package missing: {exc}")
        except IngestionJob.DoesNotExist:
            pass
        return {"job_id": job_id, "error": str(exc)}

    try:
        job = IngestionJob.objects.get(pk=job_id)
    except IngestionJob.DoesNotExist:
        log.error("Job %s not found", job_id)
        return {"job_id": job_id, "error": "job not found"}

    job.mark_started()
    log.info("Pipeline started  job=%s  url=%s", job_id, job.seed_url)

    os.environ["LLM_PROVIDER"] = job.llm_provider
    os.environ["LLM_MODEL"] = job.llm_model

    try:
        pipeline = SopIngestionPipeline(
            env_path=_ENV_PATH if _ENV_PATH.exists() else None
        )
        final_state = pipeline.run(
            job.seed_url,
            job_id=str(job.job_id),
            max_depth=job.max_depth,
            max_docs=job.max_docs,
        )
    except Exception as exc:
        log.exception("Pipeline crashed  job=%s", job_id)
        job.mark_failed(str(exc))
        return {"job_id": job_id, "error": str(exc)}

    all_docs = final_state.get("all_documents") or []
    job.docs_queued = len(all_docs)
    job.save(update_fields=["docs_queued"])

    for doc in all_docs:
        try:
            IngestedDocument.objects.update_or_create(
                job=job,
                content_hash=doc.get("content_hash", ""),
                defaults={
                    "url": doc.get("url", ""),
                    "doc_format": doc.get("doc_format", ""),
                    "depth": doc.get("depth", 0),
                    "status": doc.get("status", "OK"),
                    "neo4j_sop_id": str(doc.get("neo4j_sop_id", "") or "")[:512],
                    "pg_sop_id": str(doc.get("pg_sop_id", "") or "")[:512],
                    "steps_count": doc.get("steps_count", 0),
                    "rules_count": doc.get("rules_count", 0),
                    "codes_count": doc.get("codes_count", 0),
                    "links_found": doc.get("links_found", 0),
                },
            )
        except Exception as exc:
            log.warning("Could not save IngestedDocument: %s", exc)

    summary = final_state.get("final_summary") or {}
    errors = final_state.get("errors") or []
    job.mark_done(summary, errors)

    try:
        job.refresh_llm_totals()
    except Exception as exc:
        log.warning("refresh_llm_totals failed for job %s: %s", job_id, exc)

    # Canonical-IR authoritative write (flag-gated). Runs BEFORE auto-build so
    # the builder sees the routing-complete projection.
    _maybe_persist_ir(job, final_state)

    _maybe_auto_build_workflow(job)

    log.info(
        "Pipeline done  job=%s  processed=%s  errors=%s  llm_calls=%s  tokens_in=%s  tokens_out=%s",
        job_id,
        job.docs_processed,
        job.docs_failed,
        job.total_llm_calls,
        job.total_tokens_in,
        job.total_tokens_out,
    )
    return {"job_id": job_id, "status": job.status}


def _ir_persist_enabled() -> bool:
    """Canonical-IR persistence is ON by default (seamless high-fidelity path).

    The nested-routing IR (state["sop_ir"]) is the authoritative projection, so
    we route through ``persist_ir`` unless an operator explicitly opts out with
    ``SOP_IR_PERSIST`` set to a falsy value (``0``/``false``/``no``/``off``).
    """
    raw = os.environ.get("SOP_IR_PERSIST")
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _maybe_persist_ir(job, final_state: dict) -> None:
    """Write the canonical IR (state["sop_ir"]) into the relational audit schema
    via the shared ``persist_ir`` gate — the SAME gate the YAML importer uses.

    Flag-gated by ``SOP_IR_PERSIST``. When on, the flat ``pg_step_writer`` has
    already deferred its step/decision pass (see a11_write_postgres), so this is
    the authoritative writer: it rebuilds steps >= 1 with full routing fidelity
    (nesting, aggregation, goto_step, applicable_when, OOS) while preserving the
    synthetic Step 0 (pre-step exceptions) the pipeline wrote.

    Best-effort: a failure here is logged and never fails the ingestion job.
    """
    if not _ir_persist_enabled():
        return

    try:
        from sop_ir.persist import persist_ir
        from sop_ir.schema import SopIR

        from .models import AuditSop
    except Exception as exc:
        log.exception("persist_ir imports failed for job %s: %s", job.job_id, exc)
        return

    # Per-document IR (multi-doc crawls). Fall back to the single last-doc IR
    # for states produced before the accumulator existed.
    entries = final_state.get("sop_ir_documents") or []
    if not entries and final_state.get("sop_ir"):
        entries = [{
            "content_hash": final_state.get("content_hash", ""),
            "ir": final_state.get("sop_ir"),
            "source": final_state.get("sop_ir_source", "agentic_ingestion"),
            "validation": final_state.get("sop_ir_validation"),
            "sop_db_id": final_state.get("sop_db_id"),
        }]
    if not entries:
        log.info("persist_ir skipped: no sop_ir in final state")
        return

    for entry in entries:
        ir_data = entry.get("ir")
        if not ir_data:
            continue
        try:
            sop = None
            chash = entry.get("content_hash")
            if chash:
                sop = AuditSop.objects.filter(job=job, content_hash=chash).first()
            if sop is None and entry.get("sop_db_id"):
                sop = AuditSop.objects.filter(pk=entry["sop_db_id"]).first()
            if sop is None:
                log.warning("persist_ir: no AuditSop for job=%s content_hash=%s",
                            job.job_id, chash)
                continue
            ir = SopIR.model_validate(ir_data)
            stats = persist_ir(
                sop, ir, job=job,
                source=entry.get("source", "agentic_ingestion"),
                validation=entry.get("validation"),
                preserve_step_numbers={0},
            )
            log.info("persist_ir: sop=%s wrote %s", sop.id, stats)
        except Exception as exc:
            log.exception("persist_ir failed for job %s / content_hash %s: %s",
                          job.job_id, entry.get("content_hash"), exc)


def _maybe_auto_build_workflow(job) -> None:
    """Opt-in add-on: build the builder canvas from the ingested SOP(s).

    Only fires when the triggering Workflow set ``metadata.auto_build_canvas``
    (i.e. the create request passed ``auto_build_from_sop=true``). Best-effort:
    a failure here is logged and never fails the ingestion job.
    """
    # Reverse one-to-one access raises DoesNotExist (not AttributeError) when
    # no Workflow points at this job, so getattr(..., None) can't be used.
    try:
        workflow = job.workflow
    except ObjectDoesNotExist:
        return
    if workflow is None:
        return
    if not (workflow.metadata or {}).get("auto_build_canvas"):
        return
    try:
        from builder.sop_autobuild import build_workflow_for_job

        stats = build_workflow_for_job(workflow, job)
        log.info("auto-build workflow=%s stats=%s", workflow.id, stats)
    except Exception as exc:
        log.exception("auto-build failed for job %s / workflow %s: %s",
                      job.job_id, getattr(workflow, "id", "?"), exc)
