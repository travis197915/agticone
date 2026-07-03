"""Scheduled revision checks — probe remote SOPs and queue re-ingestion when changed."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator

from django.conf import settings
from django.utils import timezone

from uhc_sop_ingestion.revision import normalize_canonical_url, revision_dates_equal
from uhc_sop_ingestion.revision_probe import probe_remote_sop

from ..models import AuditSop, IngestionJob, JobStatus, SopDocument
from .versioning import VERSION_CONTENT_CHANGE, VERSION_REVISED, VERSION_UNCHANGED

log = logging.getLogger(__name__)

CHECK_UNCHANGED = "unchanged"
CHECK_CHANGED = "changed"
CHECK_QUEUED = "queued"
CHECK_SKIPPED_ACTIVE = "skipped_active_job"
CHECK_ERROR = "error"


@dataclass
class TrackedSop:
    document_id: int | None
    canonical_url: str
    fetch_url: str
    stored_revision_date: str
    stored_content_hash: str
    workflow_id: int | None
    max_depth: int
    max_docs: int
    llm_provider: str
    llm_model: str


def _job_defaults_from_sop(sop: AuditSop | None) -> dict[str, Any]:
    if sop and sop.job_id:
        job = sop.job
        return {
            "workflow_id": job.workflow_id,
            "max_depth": job.max_depth,
            "max_docs": job.max_docs,
            "llm_provider": job.llm_provider,
            "llm_model": job.llm_model,
        }
    from uhc_llm.backend import resolve_ingestion_job_llm

    llm_provider, llm_model = resolve_ingestion_job_llm()
    return {
        "workflow_id": None,
        "max_depth": getattr(settings, "SOP_MAX_DEPTH", 4),
        "max_docs": getattr(settings, "SOP_MAX_DOCS", 200),
        "llm_provider": llm_provider,
        "llm_model": llm_model,
    }


def tracked_sop_from_document(doc: SopDocument) -> TrackedSop:
    """Build a probe target from one ``SopDocument`` row."""
    current = doc.current_version
    defaults = _job_defaults_from_sop(current)
    canonical = normalize_canonical_url(doc.canonical_url)
    return TrackedSop(
        document_id=doc.id,
        canonical_url=canonical,
        fetch_url=current.url if current and current.url else doc.canonical_url,
        stored_revision_date=(current.revision_date if current else doc.latest_revision_date) or "",
        stored_content_hash=(current.content_hash if current else "") or "",
        **defaults,
    )


def iter_tracked_sops() -> Iterator[TrackedSop]:
    """Yield every SOP URL that should participate in scheduled revision checks."""
    seen: set[str] = set()

    docs = (
        SopDocument.objects.select_related("current_version", "current_version__job")
        .order_by("id")
    )
    for doc in docs:
        canonical = normalize_canonical_url(doc.canonical_url)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        current = doc.current_version
        defaults = _job_defaults_from_sop(current)
        yield TrackedSop(
            document_id=doc.id,
            canonical_url=canonical,
            fetch_url=current.url if current and current.url else doc.canonical_url,
            stored_revision_date=(current.revision_date if current else doc.latest_revision_date) or "",
            stored_content_hash=(current.content_hash if current else "") or "",
            **defaults,
        )

    orphan_qs = (
        AuditSop.objects.filter(is_current=True, document__isnull=True)
        .select_related("job")
        .order_by("id")
    )
    for sop in orphan_qs:
        canonical = normalize_canonical_url(sop.url)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        defaults = _job_defaults_from_sop(sop)
        yield TrackedSop(
            document_id=None,
            canonical_url=canonical,
            fetch_url=sop.url,
            stored_revision_date=sop.revision_date or "",
            stored_content_hash=sop.content_hash or "",
            **defaults,
        )


def _classify_probe(
    tracked: TrackedSop,
    probe: dict[str, Any],
) -> str:
    remote_rev = probe.get("revision_date", "") or ""
    remote_hash = probe.get("content_hash", "") or ""
    if revision_dates_equal(tracked.stored_revision_date, remote_rev):
        if tracked.stored_content_hash and remote_hash == tracked.stored_content_hash:
            return VERSION_UNCHANGED
        return VERSION_CONTENT_CHANGE
    if remote_rev or tracked.stored_revision_date:
        return VERSION_REVISED
    if tracked.stored_content_hash and remote_hash != tracked.stored_content_hash:
        return VERSION_CONTENT_CHANGE
    return VERSION_UNCHANGED


def _stale_queued_minutes() -> int:
    return int(getattr(settings, "STALE_INGESTION_JOB_QUEUED_MINUTES", 30))


def _stale_running_hours() -> int:
    return int(getattr(settings, "STALE_INGESTION_JOB_RUNNING_HOURS", 6))


def _find_active_job(canonical_url: str) -> IngestionJob | None:
    for job in (
        IngestionJob.objects.filter(status__in=[JobStatus.QUEUED, JobStatus.RUNNING])
        .order_by("-created_at")
    ):
        if normalize_canonical_url(job.seed_url) == canonical_url:
            return job
    return None


def expire_stale_job(job: IngestionJob) -> bool:
    """Fail jobs stuck in QUEUED/RUNNING. Returns True if the job was expired."""
    now = timezone.now()
    if job.status == JobStatus.QUEUED and not job.started_at:
        age_s = (now - job.created_at).total_seconds()
        if age_s > _stale_queued_minutes() * 60:
            job.mark_failed(
                f"Stale queued job expired after {_stale_queued_minutes()}m without starting",
            )
            log.warning("Expired stale QUEUED job %s  url=%s", job.job_id, job.seed_url[:80])
            return True
    if job.status == JobStatus.RUNNING:
        ref = job.started_at or job.created_at
        age_s = (now - ref).total_seconds()
        if age_s > _stale_running_hours() * 3600:
            job.mark_failed(
                f"Stale running job expired after {_stale_running_hours()}h",
            )
            log.warning("Expired stale RUNNING job %s  url=%s", job.job_id, job.seed_url[:80])
            return True
    return False


def _resolve_blocking_job(
    canonical_url: str,
    *,
    force: bool = False,
) -> tuple[IngestionJob | None, str | None]:
    """Return a blocking job if re-ingest must wait; expire stale jobs first."""
    job = _find_active_job(canonical_url)
    if not job:
        return None, None
    if expire_stale_job(job):
        return None, str(job.job_id)
    if force:
        job.mark_failed("Superseded by forced revision-check re-ingest")
        log.info("Force-failed blocking job %s for url=%s", job.job_id, canonical_url)
        return None, str(job.job_id)
    return job, None


def _update_document_check(
    document_id: int | None,
    *,
    status: str,
    probe: dict[str, Any] | None,
    detail: str = "",
) -> None:
    if not document_id:
        return
    fields = {
        "last_revision_check_at": timezone.now(),
        "last_revision_check_status": status,
        "last_revision_check_detail": detail[:2000],
    }
    if probe:
        fields["last_remote_revision_date"] = probe.get("revision_date", "") or ""
        fields["last_remote_content_hash"] = probe.get("content_hash", "") or ""
    SopDocument.objects.filter(pk=document_id).update(**fields)


def check_tracked_sop(
    tracked: TrackedSop,
    *,
    dispatch: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Probe one tracked SOP and optionally queue a full ingestion job."""
    probe = probe_remote_sop(tracked.fetch_url)
    if not probe.get("ok"):
        _update_document_check(
            tracked.document_id,
            status=CHECK_ERROR,
            probe=None,
            detail=probe.get("error", "probe failed"),
        )
        return {
            "canonical_url": tracked.canonical_url,
            "document_id": tracked.document_id,
            "status": CHECK_ERROR,
            "error": probe.get("error"),
        }

    version_action = _classify_probe(tracked, probe)
    remote_hash = probe.get("content_hash", "") or ""
    result = {
        "canonical_url": tracked.canonical_url,
        "document_id": tracked.document_id,
        "status": CHECK_UNCHANGED,
        "version_action": version_action,
        "has_deviation": version_action != VERSION_UNCHANGED,
        "remote_revision_date": probe.get("revision_date", ""),
        "stored_revision_date": tracked.stored_revision_date,
        "remote_content_hash": remote_hash,
        "stored_content_hash": tracked.stored_content_hash,
    }

    if version_action == VERSION_UNCHANGED:
        _update_document_check(
            tracked.document_id,
            status=CHECK_UNCHANGED,
            probe=probe,
            detail="revision date and content hash unchanged",
        )
        return result

    if dispatch:
        blocking, expired_job_id = _resolve_blocking_job(tracked.canonical_url, force=force)
        if expired_job_id:
            result["expired_blocking_job_id"] = expired_job_id
        if blocking:
            _update_document_check(
                tracked.document_id,
                status=CHECK_SKIPPED_ACTIVE,
                probe=probe,
                detail=(
                    f"ingestion already {blocking.status.lower()} "
                    f"(job {blocking.job_id}); use ?force=1 to supersede"
                ),
            )
            result["status"] = CHECK_SKIPPED_ACTIVE
            result["blocking_job_id"] = str(blocking.job_id)
            result["blocking_job_status"] = blocking.status
            result["message"] = (
                "Re-ingest blocked by an existing queued/running job for this URL. "
                "Retry with ?force=1 after confirming the blocking job is stale, "
                "or wait for it to finish."
            )
            return result

    result["status"] = CHECK_CHANGED
    if not dispatch:
        _update_document_check(
            tracked.document_id,
            status=CHECK_CHANGED,
            probe=probe,
            detail=f"would queue ingestion ({version_action})",
        )
        return result

    job = IngestionJob.objects.create(
        workflow_id=tracked.workflow_id,
        seed_url=tracked.fetch_url,
        max_depth=tracked.max_depth,
        max_docs=tracked.max_docs,
        llm_provider=tracked.llm_provider,
        llm_model=tracked.llm_model,
        trigger_source="revision_check",
    )
    from ..tasks import run_ingestion_pipeline

    task = run_ingestion_pipeline.delay(str(job.job_id))
    job.celery_task_id = task.id
    job.save(update_fields=["celery_task_id"])

    _update_document_check(
        tracked.document_id,
        status=CHECK_QUEUED,
        probe=probe,
        detail=f"queued job {job.job_id} ({version_action}); pending human review",
    )
    result.update({
        "status": CHECK_QUEUED,
        "job_id": str(job.job_id),
        "celery_task_id": task.id,
        "requires_human_review": True,
        "message": (
            "Re-ingestion queued. New version will be saved as pending_review "
            "until POST /api/ingest/sops/<sop_id>/activate/."
        ),
    })
    log.info(
        "Revision check queued re-ingest  url=%s  action=%s  job=%s",
        tracked.canonical_url,
        version_action,
        job.job_id,
    )
    return result


def run_revision_check_batch(*, dispatch: bool = True) -> dict[str, Any]:
    """Check every tracked SOP; queue ingestion jobs when revision/content changed."""
    results: list[dict[str, Any]] = []
    summary = {
        "checked": 0,
        "unchanged": 0,
        "changed": 0,
        "queued": 0,
        "skipped_active_job": 0,
        "errors": 0,
    }

    for tracked in iter_tracked_sops():
        summary["checked"] += 1
        item = check_tracked_sop(tracked, dispatch=dispatch)
        results.append(item)
        status = item.get("status")
        if status == CHECK_UNCHANGED:
            summary["unchanged"] += 1
        elif status == CHECK_CHANGED:
            summary["changed"] += 1
        elif status == CHECK_QUEUED:
            summary["queued"] += 1
            summary["changed"] += 1
        elif status == CHECK_SKIPPED_ACTIVE:
            summary["skipped_active_job"] += 1
            summary["changed"] += 1
        elif status == CHECK_ERROR:
            summary["errors"] += 1

    log.info("Revision check batch complete  summary=%s", summary)
    return {"summary": summary, "results": results}
