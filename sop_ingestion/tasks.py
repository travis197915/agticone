"""Celery master tasks — dispatch only; pipeline runs in subprocesses."""
from __future__ import annotations

import logging

from celery import shared_task

from .models import IngestionJob
from .subprocess_manager import spawn_ingestion_subprocess

log = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=0, name="sop_ingestion.run_pipeline")
def run_ingestion_pipeline(self, job_id: str) -> dict:
    """Master dispatcher: spawn an OS subprocess; do not run LangGraph here."""
    try:
        IngestionJob.objects.get(pk=job_id)
    except IngestionJob.DoesNotExist:
        log.error("Job %s not found — not spawning subprocess", job_id)
        return {"job_id": job_id, "error": "job not found"}

    try:
        pid = spawn_ingestion_subprocess(job_id)
    except Exception as exc:
        log.exception("Subprocess dispatch failed for job %s", job_id)
        try:
            job = IngestionJob.objects.get(pk=job_id)
            job.mark_failed(f"Subprocess dispatch failed: {exc}")
        except IngestionJob.DoesNotExist:
            pass
        return {"job_id": job_id, "error": str(exc)}

    log.info("Dispatched job %s to subprocess pid=%s (celery_task=%s)",
             job_id, pid, self.request.id)
    return {
        "job_id": job_id,
        "spawned": True,
        "subprocess_pid": pid,
        "celery_task_id": self.request.id,
    }


@shared_task(bind=True, max_retries=0, name="sop_ingestion.contextualize_job")
def run_narrative_contextualizer(self, job_id: str) -> dict:
    """Run narrative agents on already-ingested SOPs for a job.

    Lighter than full ingestion; still runs in the Celery worker process.
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
