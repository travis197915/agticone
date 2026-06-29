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


@shared_task(bind=True, max_retries=0, name="sop_ingestion.reconcile_analyze")
def reconcile_analyze_task(self, sop_id: int, yaml_text: str,
                           source: str = "pasted") -> dict:
    """AI-compare an uploaded SOP YAML against a SOP's rules, in the worker.

    The compare loop makes one LLM call per *changed* rule, so it can run for
    many seconds on a large SOP — running it here keeps the web server free.
    Progress is streamed via Celery ``update_state`` (PROGRESS) so the dialog
    can poll ``reconcile/status`` and show a bar. Returns the full analyze
    result dict (the findings table) on success.
    """
    from .models import AuditSop
    from . import rule_reconcile

    try:
        sop = AuditSop.objects.get(pk=sop_id)
    except AuditSop.DoesNotExist:
        return {"error": "sop not found", "sop_id": sop_id}

    def _progress(processed: int, total: int, phase: str) -> None:
        self.update_state(state="PROGRESS", meta={
            "processed": processed, "total": total, "phase": phase,
            "sop_id": sop_id,
        })

    try:
        result = rule_reconcile.analyze(
            sop, yaml_text, source=source, progress_cb=_progress)
    except ValueError as exc:
        # Bad YAML — surface as a clean validation error the UI can show.
        return {"error": str(exc), "error_kind": "validation", "sop_id": sop_id}
    except Exception as exc:  # pragma: no cover
        log.exception("reconcile_analyze failed  sop=%s", sop_id)
        return {"error": str(exc), "error_kind": "internal", "sop_id": sop_id}

    log.info("reconcile_analyze done  sop=%s  findings=%d",
             sop_id, len(result.get("findings", [])))
    return result


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
