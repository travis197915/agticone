"""Tests for pending-review activation workflow."""
from django.test import TestCase

from sop_ingestion.models import ActivationStatus, AuditSop, IngestionJob, SopDocument
from sop_ingestion.services.versioning import (
    VERSION_REVISED,
    activate_sop_version,
    register_sop_version,
    reject_sop_version,
)


class VersionActivationTests(TestCase):
    def setUp(self):
        job_old = IngestionJob.objects.create(seed_url="https://example.com/sop.html")
        job_new = IngestionJob.objects.create(
            seed_url="https://example.com/sop.html",
            trigger_source="revision_check",
        )
        self.doc = SopDocument.objects.create(
            canonical_url="https://example.com/sop.html",
            title="Example SOP",
            latest_revision_date="01/15/2024",
        )
        self.current = AuditSop.objects.create(
            job=job_old,
            document=self.doc,
            url="https://example.com/sop.html",
            content_hash="hash-old",
            revision_date="01/15/2024",
            is_current=True,
            activation_status=ActivationStatus.ACTIVE,
            version_number=1,
        )
        self.pending = AuditSop.objects.create(
            job=job_new,
            url="https://example.com/sop.html",
            content_hash="hash-new",
            revision_date="06/01/2025",
            is_current=False,
            activation_status=ActivationStatus.ACTIVE,
            version_number=1,
        )
        self.doc.current_version = self.current
        self.doc.save(update_fields=["current_version"])

    def test_register_pending_review_keeps_current_active(self):
        register_sop_version(
            self.pending.id,
            prior_sop_id=self.current.id,
            version_action=VERSION_REVISED,
            canonical_url=self.doc.canonical_url,
            revision_date="06/01/2025",
            auto_activate=False,
        )
        self.current.refresh_from_db()
        self.pending.refresh_from_db()
        self.doc.refresh_from_db()

        assert self.current.is_current is True
        assert self.current.activation_status == ActivationStatus.ACTIVE
        assert self.pending.is_current is False
        assert self.pending.activation_status == ActivationStatus.PENDING_REVIEW
        assert self.pending.version_number == 2
        assert self.doc.current_version_id == self.current.id

    def test_activate_promotes_pending_version(self):
        register_sop_version(
            self.pending.id,
            prior_sop_id=self.current.id,
            version_action=VERSION_REVISED,
            canonical_url=self.doc.canonical_url,
            revision_date="06/01/2025",
            auto_activate=False,
        )
        activated = activate_sop_version(self.pending.id, reviewed_by="auditor@example.com")

        self.current.refresh_from_db()
        self.doc.refresh_from_db()
        assert activated.is_current is True
        assert activated.activation_status == ActivationStatus.ACTIVE
        assert self.current.is_current is False
        assert self.current.activation_status == ActivationStatus.SUPERSEDED
        assert self.doc.current_version_id == self.pending.id

    def test_reject_pending_version(self):
        register_sop_version(
            self.pending.id,
            prior_sop_id=self.current.id,
            version_action=VERSION_REVISED,
            canonical_url=self.doc.canonical_url,
            revision_date="06/01/2025",
            auto_activate=False,
        )
        rejected = reject_sop_version(self.pending.id, reason="bad parse")

        self.current.refresh_from_db()
        self.doc.refresh_from_db()
        assert rejected.activation_status == ActivationStatus.REJECTED
        assert rejected.is_current is False
        assert self.current.is_current is True
        assert self.doc.current_version_id == self.current.id
