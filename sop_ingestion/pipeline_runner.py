"""Run one SOP ingestion job in a standalone process (subprocess slave).

Called from ``sop_ingestion/worker/job_runner.py`` — not from the Celery master.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

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
        pipeline = SopIngestionPipeline(env_path=_ENV_PATH)
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
                    "neo4j_sop_id": doc.get("neo4j_sop_id", ""),
                    "pg_sop_id": doc.get("pg_sop_id", ""),
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
