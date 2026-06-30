"""Tests for SOP approval checks on workflow bindings."""
from __future__ import annotations

import uuid

from django.test import TestCase

from agent_tools.models import NodeRuleBinding
from builder.models import (
    Shape, ShapeCategory, ShapeDefinition, WorkArea, Workbench, Workflow,
)
from builder.sop_compliance import sop_approval_meta, workflow_binding_compliance
from sop_ingestion.models import AuditSop, IngestionJob, SopDocument


def _shape() -> Shape:
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
    return Shape.objects.create(workbench=wb, definition=sd, label="S")


class SopComplianceTests(TestCase):
    def setUp(self):
        self.doc = SopDocument.objects.create(
            canonical_url="https://example.com/sop.html",
            title="SOP",
        )
        self.job = IngestionJob.objects.create(
            seed_url=self.doc.canonical_url,
            status="COMPLETED",
        )
        self.active = AuditSop.objects.create(
            job=self.job,
            document=self.doc,
            url=self.doc.canonical_url,
            content_hash="a",
            version_number=1,
            is_current=True,
            activation_status="active",
        )
        self.doc.current_version = self.active
        self.doc.save(update_fields=["current_version"])
        self.pending = AuditSop.objects.create(
            job=self.job,
            document=self.doc,
            url=self.doc.canonical_url,
            content_hash="b",
            version_number=2,
            is_current=False,
            activation_status="pending_review",
        )

    def test_active_version_is_approved(self):
        meta = sop_approval_meta(self.active)
        self.assertTrue(meta["is_approved"])
        self.assertIsNone(meta["approval_issue"])

    def test_pending_version_not_approved(self):
        meta = sop_approval_meta(self.pending)
        self.assertFalse(meta["is_approved"])
        self.assertIn("pending_review", meta["approval_issue"])

    def test_workflow_binding_compliance_flags_stale(self):
        shape = _shape()
        wf = shape.workbench.work_area.workflow
        NodeRuleBinding.objects.create(
            shape=shape,
            rule_key=f"pre:{self.pending.id}:1",
            sop=self.pending,
        )
        summary = workflow_binding_compliance(wf)
        self.assertTrue(summary["has_unapproved_bindings"])
        self.assertEqual(summary["unapproved_binding_count"], 1)
        self.assertEqual(summary["unapproved_sop_ids"], [self.pending.id])
