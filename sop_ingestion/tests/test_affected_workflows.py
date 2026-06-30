"""Tests for GET /api/ingest/documents/{id}/affected-workflows/."""
from __future__ import annotations

import uuid

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from agent_tools.models import NodeRuleBinding
from builder.auth import CorebackendUser
from builder.models import (
    Shape, ShapeCategory, ShapeDefinition, WorkArea, Workbench, Workflow,
)
from sop_ingestion.models import AuditSop, IngestionJob, SopDocument


def _make_shape(label: str = "Shape A") -> Shape:
    wf = Workflow.objects.create(
        name=f"WF-{uuid.uuid4().hex[:8]}",
        slug=f"wf-{uuid.uuid4().hex[:8]}",
        description="",
        is_active=True,
    )
    wa = WorkArea.objects.create(workflow=wf, name="WA", order=0)
    wb = Workbench.objects.create(work_area=wa, name="WB", order=0)
    cat, _ = ShapeCategory.objects.get_or_create(
        slug="test-cat",
        defaults={"label": "Test", "order": 0, "is_active": True},
    )
    sd, _ = ShapeDefinition.objects.get_or_create(
        slug="test-decision",
        defaults={
            "category": cat,
            "label": "Test Decision",
            "kind": "rectangle",
            "svg_path": "M0 0h100v100H0z",
        },
    )
    return Shape.objects.create(workbench=wb, definition=sd, label=label)


class AffectedWorkflowsApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        user = CorebackendUser(id="test-user", email="t@example.com", role="MEMBER")
        self.client.force_authenticate(user=user)

        self.doc = SopDocument.objects.create(
            canonical_url="https://example.com/sop.html",
            title="Test SOP",
        )
        self.job = IngestionJob.objects.create(
            seed_url=self.doc.canonical_url,
            status="COMPLETED",
            workflow_id=None,
        )
        self.sop_v1 = AuditSop.objects.create(
            job=self.job,
            document=self.doc,
            title="Test SOP",
            url=self.doc.canonical_url,
            content_hash="hash-v1",
            doc_format="HTML",
            version_number=1,
            is_current=True,
            activation_status="active",
        )
        self.doc.current_version = self.sop_v1
        self.doc.save(update_fields=["current_version"])
        self.sop_v2 = AuditSop.objects.create(
            job=self.job,
            document=self.doc,
            title="Test SOP",
            url=self.doc.canonical_url,
            content_hash="hash-v2",
            doc_format="HTML",
            version_number=2,
            is_current=False,
            activation_status="pending_review",
        )
        self.shape = _make_shape()
        self.workflow = self.shape.workbench.work_area.workflow
        NodeRuleBinding.objects.create(
            shape=self.shape,
            rule_key=f"pre:{self.sop_v1.id}:rule-1",
            sop=self.sop_v1,
        )

    def test_lists_workflow_with_rule_binding(self):
        url = reverse(
            "sop_ingestion:sop-document-affected-workflows",
            kwargs={"document_id": self.doc.id},
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["document_id"], self.doc.id)
        self.assertEqual(data["summary"]["rule_binding_count"], 1)
        self.assertEqual(len(data["workflows"]), 1)
        wf = data["workflows"][0]
        self.assertEqual(wf["workflow_id"], str(self.workflow.id))
        self.assertEqual(len(wf["shapes"]), 1)
        self.assertEqual(wf["shapes"][0]["rules"][0]["sop_id"], self.sop_v1.id)
        self.assertFalse(wf["needs_rebind_after_version_change"])

    def test_filter_by_sop_id(self):
        url = reverse(
            "sop_ingestion:sop-document-affected-workflows",
            kwargs={"document_id": self.doc.id},
        )
        resp = self.client.get(url, {"sop_id": self.sop_v2.id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["workflows"], [])

        resp = self.client.get(url, {"sop_id": self.sop_v1.id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["workflows"]), 1)

    def test_current_only_excludes_non_current_bindings(self):
        NodeRuleBinding.objects.create(
            shape=self.shape,
            rule_key=f"pre:{self.sop_v2.id}:rule-2",
            sop=self.sop_v2,
        )
        url = reverse(
            "sop_ingestion:sop-document-affected-workflows",
            kwargs={"document_id": self.doc.id},
        )
        resp = self.client.get(url, {"current_only": "1"})
        self.assertEqual(resp.status_code, 200)
        rules = resp.json()["workflows"][0]["shapes"][0]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["sop_id"], self.sop_v1.id)

    def test_needs_rebind_when_binding_differs_from_current_version(self):
        self.doc.current_version = self.sop_v2
        self.doc.save(update_fields=["current_version"])
        url = reverse(
            "sop_ingestion:sop-document-affected-workflows",
            kwargs={"document_id": self.doc.id},
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["workflows"][0]["needs_rebind_after_version_change"])

    def test_invalid_sop_id_returns_404(self):
        url = reverse(
            "sop_ingestion:sop-document-affected-workflows",
            kwargs={"document_id": self.doc.id},
        )
        resp = self.client.get(url, {"sop_id": 99999})
        self.assertEqual(resp.status_code, 404)
