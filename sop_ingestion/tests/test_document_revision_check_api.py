"""API tests for per-document revision drift checks."""
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from builder.auth import CorebackendUser
from sop_ingestion.models import AuditSop, IngestionJob, SopDocument


class DocumentRevisionCheckApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        user = CorebackendUser(id=1, username="tester", email="t@example.com")
        self.client.force_authenticate(user=user)

        job = IngestionJob.objects.create(
            seed_url="https://example.com/sop.html",
            max_depth=2,
            max_docs=10,
        )
        self.doc = SopDocument.objects.create(
            canonical_url="https://example.com/sop.html",
            title="Example SOP",
            latest_revision_date="01/15/2024",
        )
        sop = AuditSop.objects.create(
            job=job,
            document=self.doc,
            url="https://example.com/sop.html",
            content_hash="abc123",
            revision_date="01/15/2024",
            is_current=True,
            version_number=1,
        )
        self.doc.current_version = sop
        self.doc.save(update_fields=["current_version"])
        self.url = reverse(
            "sop_ingestion:sop-document-revision-check",
            kwargs={"document_id": self.doc.id},
        )

    @patch("sop_ingestion.services.revision_scheduler.probe_remote_sop")
    def test_unchanged(self, mock_probe):
        mock_probe.return_value = {
            "ok": True,
            "revision_date": "01/15/2024",
            "content_hash": "abc123",
        }
        response = self.client.post(f"{self.url}?dry_run=1")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["document_id"], self.doc.id)
        self.assertFalse(data["has_deviation"])
        self.assertEqual(data["version_action"], "UNCHANGED")
        self.assertEqual(data["status"], "unchanged")

    @patch("sop_ingestion.services.revision_scheduler.probe_remote_sop")
    def test_detects_revision_drift(self, mock_probe):
        mock_probe.return_value = {
            "ok": True,
            "revision_date": "06/01/2025",
            "content_hash": "def456",
        }
        response = self.client.post(f"{self.url}?dry_run=1")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["has_deviation"])
        self.assertEqual(data["version_action"], "REVISED")
        self.assertEqual(data["status"], "changed")

    def test_not_found(self):
        missing_url = reverse(
            "sop_ingestion:sop-document-revision-check",
            kwargs={"document_id": 999999},
        )
        response = self.client.post(missing_url)
        self.assertEqual(response.status_code, 404)
