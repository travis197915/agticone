"""Tests for stale ingestion job expiry during revision checks."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from sop_ingestion.models import IngestionJob, JobStatus, SopDocument
from sop_ingestion.services.revision_scheduler import (
    CHECK_QUEUED,
    CHECK_SKIPPED_ACTIVE,
    check_tracked_sop,
    expire_stale_job,
    tracked_sop_from_document,
)


class StaleJobExpiryTests(TestCase):
    def setUp(self):
        self.doc = SopDocument.objects.create(
            canonical_url="http://127.0.0.1:8080/OBH_Facets_Duplicate_Claim_Handling.html",
            title="Example",
            latest_revision_date="09/12/2025",
        )

    def test_expire_stale_queued_job(self):
        job = IngestionJob.objects.create(
            seed_url=self.doc.canonical_url,
            status=JobStatus.QUEUED,
        )
        IngestionJob.objects.filter(pk=job.pk).update(
            created_at=timezone.now() - timedelta(hours=2),
        )
        job.refresh_from_db()
        assert expire_stale_job(job) is True
        job.refresh_from_db()
        assert job.status == JobStatus.FAILED

    @patch("sop_ingestion.services.revision_scheduler.probe_remote_sop")
    @patch("sop_ingestion.services.revision_scheduler.run_ingestion_pipeline")
    def test_stale_blocking_job_auto_expires_then_queues(self, mock_delay, mock_probe):
        stale = IngestionJob.objects.create(
            seed_url=self.doc.canonical_url,
            status=JobStatus.QUEUED,
        )
        IngestionJob.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(hours=2),
        )
        mock_probe.return_value = {
            "ok": True,
            "revision_date": "09/02/2026",
            "content_hash": "newhash123",
        }
        mock_delay.delay.return_value = type("T", (), {"id": "celery-1"})()

        result = check_tracked_sop(tracked_sop_from_document(self.doc), dispatch=True)

        stale.refresh_from_db()
        assert stale.status == JobStatus.FAILED
        assert result["status"] == CHECK_QUEUED
        assert "job_id" in result

    @patch("sop_ingestion.services.revision_scheduler.probe_remote_sop")
    def test_active_job_blocks_until_force(self, mock_probe):
        blocking = IngestionJob.objects.create(
            seed_url=self.doc.canonical_url,
            status=JobStatus.RUNNING,
            started_at=timezone.now(),
        )
        mock_probe.return_value = {
            "ok": True,
            "revision_date": "09/02/2026",
            "content_hash": "newhash123",
        }

        blocked = check_tracked_sop(tracked_sop_from_document(self.doc), dispatch=True)
        assert blocked["status"] == CHECK_SKIPPED_ACTIVE
        assert blocked["blocking_job_id"] == str(blocking.job_id)

        forced = check_tracked_sop(
            tracked_sop_from_document(self.doc),
            dispatch=True,
            force=True,
        )
        blocking.refresh_from_db()
        assert blocking.status == JobStatus.FAILED
        assert forced["status"] == CHECK_QUEUED
