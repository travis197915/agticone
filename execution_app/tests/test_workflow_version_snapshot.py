"""Tests for RuleExecutionRun.workflow_version_snapshot (builder.WorkflowVersion
FK) and its serialization — the durable, structured record of exactly which
historical workflow composition a claim ran against.

See builder/tests/test_workflow_versioning.py for the note on why these are
verified via direct ORM/serializer assertions rather than `manage.py test`
end-to-end right now (pre-existing, unrelated test-DB migration drift on
this branch). RuleExecutionRun instances here are built in-memory (never
`.save()`d) for the same reason the rest of this app's tests avoid a full
persist where possible — a separate pre-existing NOT NULL column
(`rerun_scope`) not declared on the model raises on INSERT; serialization
does not require a DB round-trip.
"""
from __future__ import annotations

import uuid

from django.test import TestCase

from builder.models import Workbench, WorkArea, Workflow
from builder.workflow_versioning import snapshot_workflow_version
from execution_app.models import RuleExecutionRun
from execution_app.serializers import RuleExecutionRunSerializer


class WorkflowVersionSnapshotSerializationTests(TestCase):
    def setUp(self):
        self.wf = Workflow.objects.create(
            name=f"WF-{uuid.uuid4().hex[:8]}",
            slug=f"wf-{uuid.uuid4().hex[:8]}",
            description="", is_active=True,
        )
        area = WorkArea.objects.create(workflow=self.wf, name="Claim Audit", order=0)
        Workbench.objects.create(
            work_area=area, name="1. SOP A", order=0, node_key="a",
            version=1, is_current=True,
            config={"sop_id": 1, "sop_title": "SOP A",
                    "source_url": "https://example.com/a", "content_hash": "a1"},
        )
        self.snapshot = snapshot_workflow_version(self.wf, reason="initial_build")

    def test_nested_composition_serializes_from_the_snapshot(self):
        run = RuleExecutionRun(
            id=uuid.uuid4(), workflow=self.wf, claim_id="C-1",
            workflow_version=1, workflow_version_snapshot=self.snapshot,
        )
        data = RuleExecutionRunSerializer(run).data

        self.assertIsNotNone(data["workflow_version_snapshot"])
        self.assertEqual(data["workflow_version_snapshot"]["workflow_version"], 1)
        self.assertEqual(len(data["workflow_version_snapshot"]["sops"]), 1)
        sop = data["workflow_version_snapshot"]["sops"][0]
        self.assertEqual(sop["node_key"], "a")
        self.assertEqual(sop["sop_title"], "SOP A")

    def test_null_when_unresolved(self):
        run = RuleExecutionRun(
            id=uuid.uuid4(), workflow=self.wf, claim_id="C-2",
            workflow_version=None, workflow_version_snapshot=None,
        )
        data = RuleExecutionRunSerializer(run).data
        self.assertIsNone(data["workflow_version_snapshot"])

    def test_old_run_keeps_its_snapshot_after_workflow_moves_on(self):
        """The headline guarantee, exercised through the FK + serializer:
        once a run is (conceptually) persisted against a WorkflowVersion,
        later composition changes on the workflow must not change what that
        run reports."""
        run = RuleExecutionRun(
            id=uuid.uuid4(), workflow=self.wf, claim_id="C-3",
            workflow_version=1, workflow_version_snapshot=self.snapshot,
        )

        # Workflow moves to v2 (new Workbench for slot "a").
        old = Workbench.objects.get(work_area__workflow=self.wf, node_key="a")
        Workbench.objects.create(
            work_area=old.work_area, name=old.name, order=old.order,
            node_key="a", version=2, is_current=True,
            config={**old.config, "content_hash": "a2"},
        )
        Workbench.objects.filter(pk=old.pk).update(is_current=False)
        snapshot_workflow_version(self.wf, reason="sop_content_changed")
        self.wf.refresh_from_db()
        self.assertEqual(self.wf.version, 2)

        # The run object still points at the original (v1) snapshot.
        data = RuleExecutionRunSerializer(run).data
        self.assertEqual(data["workflow_version_snapshot"]["workflow_version"], 1)
        self.assertEqual(
            data["workflow_version_snapshot"]["sops"][0]["workbench_version"], 1,
        )
