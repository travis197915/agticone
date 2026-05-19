"""Celery task — runs the uhc-sop-ingestion LangGraph pipeline."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from celery import shared_task
from .models import IngestionJob, IngestedDocument

log = logging.getLogger(__name__)

# .env lives in the project root (same folder as manage.py)
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


@shared_task(bind=True, max_retries=0, name="sop_ingestion.run_pipeline")
def run_ingestion_pipeline(self, job_id: str) -> dict:
    # ── Import pipeline ───────────────────────────────────────────────────────
    try:
        from uhc_sop_ingestion.pipeline import SopIngestionPipeline
    except ImportError as exc:
        log.error("uhc-sop-ingestion not installed: %s", exc)
        try:
            IngestionJob.objects.get(pk=job_id).mark_failed(f"Package missing: {exc}")
        except IngestionJob.DoesNotExist:
            pass
        return {"error": str(exc)}

    # ── Fetch job record ──────────────────────────────────────────────────────
    try:
        job = IngestionJob.objects.get(pk=job_id)
    except IngestionJob.DoesNotExist:
        log.error("Job %s not found", job_id)
        return {"error": "job not found"}

    job.mark_started()
    log.info("Pipeline started  job=%s  url=%s", job_id, job.seed_url)

    # Pass LLM choice into environment so PipelineConfig.from_env() picks them up.
    # max_depth / max_docs are forwarded as direct arguments to pipeline.run().
    os.environ["LLM_PROVIDER"] = job.llm_provider
    os.environ["LLM_MODEL"]    = job.llm_model

    # ── Run ───────────────────────────────────────────────────────────────────
    try:
        pipeline    = SopIngestionPipeline(env_path=_ENV_PATH)
        final_state = pipeline.run(
            job.seed_url,
            job_id=str(job.job_id),
            max_depth=job.max_depth,
            max_docs=job.max_docs,
        )
    except Exception as exc:
        log.exception("Pipeline crashed  job=%s", job_id)
        job.mark_failed(str(exc))
        return {"error": str(exc)}

    # ── Persist per-document rows ─────────────────────────────────────────────
    all_docs = final_state.get("all_documents") or []
    job.docs_queued = len(all_docs)
    job.save(update_fields=["docs_queued"])

    for doc in all_docs:
        try:
            IngestedDocument.objects.update_or_create(
                job=job,
                content_hash=doc.get("content_hash", ""),
                defaults={
                    "url":          doc.get("url", ""),
                    "doc_format":   doc.get("doc_format", ""),
                    "depth":        doc.get("depth", 0),
                    "status":       doc.get("status", "OK"),
                    "neo4j_sop_id": doc.get("neo4j_sop_id", ""),
                    "pg_sop_id":    doc.get("pg_sop_id", ""),
                    "steps_count":  doc.get("steps_count", 0),
                    "rules_count":  doc.get("rules_count", 0),
                    "codes_count":  doc.get("codes_count", 0),
                    "links_found":  doc.get("links_found", 0),
                },
            )
        except Exception as exc:
            log.warning("Could not save IngestedDocument: %s", exc)

    # ── Close job ─────────────────────────────────────────────────────────────
    summary = final_state.get("final_summary") or {}
    errors  = final_state.get("errors") or []
    job.mark_done(summary, errors)

    # Sync LLM token aggregates from LLMCallLog rows written during the run.
    try:
        job.refresh_llm_totals()
    except Exception as exc:
        log.warning("refresh_llm_totals failed for job %s: %s", job_id, exc)

    log.info("Pipeline done  job=%s  processed=%s  errors=%s  llm_calls=%s  tokens_in=%s  tokens_out=%s",
             job_id, job.docs_processed, job.docs_failed,
             job.total_llm_calls, job.total_tokens_in, job.total_tokens_out)
    return {"job_id": job_id, "status": job.status}


@shared_task(bind=True, max_retries=0, name="sop_ingestion.contextualize_job")
def run_narrative_contextualizer(self, job_id: str) -> dict:
    """Run narrative agents on already-ingested SOPs for a job.

    Use for back-filling: existing AuditSop rows from before the
    narrative stage existed, or whose ``narrative_context`` is blank
    because of a previous LLM failure.  Does **not** re-fetch the
    source HTML — it rebuilds state from Postgres.
    """
    from .services.contextualizer import contextualize_job
    try:
        results = contextualize_job(job_id)
        log.info("Narrative contextualizer done  job=%s  sops=%d",
                 job_id, len(results))
        return {"job_id": job_id, "results": results}
    except Exception as exc:
        log.exception("Narrative contextualizer failed  job=%s", job_id)
        return {"job_id": job_id, "error": str(exc)}
