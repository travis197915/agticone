"""
Model-level smoke tests for the agent_tools binding tables.

These exercise the invariants the registry/UI depend on:

* :class:`Tool` rows survive an upsert by name (used by the registry seed
  data migration and the ``sync_tool_registry`` management command).
* :class:`NodeRuleBinding` uniqueness is ``(shape, rule_key)`` — re-attaching
  the same rule to the same shape must error rather than duplicating.
* :class:`NodeToolBinding` ``rule_binding`` is optional and FK-nullable so
  tools picked outside a rule context still land in the table.

The tests use Django's ``TestCase`` so each runs in its own transaction;
they don't require any external services beyond the configured Postgres
database.
"""
from __future__ import annotations

import uuid

from django.db import IntegrityError
from django.test import TestCase

from agent_tools.models import NodeRuleBinding, NodeToolBinding, Tool
from builder.models import (
    Shape, ShapeCategory, ShapeDefinition, WorkArea, Workbench, Workflow,
)
from sop_ingestion.models import AuditSop, IngestionJob


def _make_audit_sop(title: str) -> AuditSop:
    """Build an AuditSop with the minimum scaffolding (an IngestionJob)."""
    job = IngestionJob.objects.create(
        seed_url=f"https://example.com/{title.lower()}.html",
        status="COMPLETED",
    )
    return AuditSop.objects.create(
        job=job,
        title=title,
        url=f"https://example.com/{title.lower()}.html",
        content_hash=uuid.uuid4().hex,
        doc_format="HTML",
    )


class _ShapeFixtureMixin:
    """Build the minimum builder-side scaffolding a Shape needs to exist."""

    @classmethod
    def _make_shape(cls, label: str = "Shape A") -> Shape:
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
                "label":    "Test Decision",
                "kind":     "rectangle",
                "svg_path": "M0 0h100v100H0z",
            },
        )
        return Shape.objects.create(workbench=wb, definition=sd, label=label)


class ToolUpsertTests(TestCase):
    """sync_to_db() must be idempotent — re-runs upsert by name."""

    def test_create_and_upsert_by_name(self):
        # Use a unique-to-this-test name so the seed migration (which
        # pre-inserts the 18 real LangChain tools) doesn't collide.
        name = "test_only_upsert_subject"
        Tool.objects.create(
            name=name,
            display_name="Initial",
            description="initial",
            kind=Tool.KIND_LANGCHAIN,
        )
        Tool.objects.update_or_create(
            name=name,
            defaults={"display_name": "Updated", "description": "updated"},
        )
        self.assertEqual(Tool.objects.filter(name=name).count(), 1)
        row = Tool.objects.get(name=name)
        self.assertEqual(row.display_name, "Updated")
        self.assertEqual(row.description, "updated")

    def test_name_is_unique(self):
        Tool.objects.create(name="dup", display_name="A", kind=Tool.KIND_LANGCHAIN)
        with self.assertRaises(IntegrityError):
            Tool.objects.create(
                name="dup", display_name="B", kind=Tool.KIND_LANGCHAIN,
            )


class NodeRuleBindingTests(_ShapeFixtureMixin, TestCase):
    """The (shape, rule_key) constraint and shape-cascade behaviour."""

    @classmethod
    def setUpTestData(cls):
        cls.shape = cls._make_shape("Rule host")
        cls.sop = _make_audit_sop("SOP-A")

    def test_unique_shape_rule_key(self):
        NodeRuleBinding.objects.create(
            shape=self.shape, sop=self.sop, rule_key="step:1:0:0",
        )
        with self.assertRaises(IntegrityError):
            NodeRuleBinding.objects.create(
                shape=self.shape, sop=self.sop, rule_key="step:1:0:0",
            )

    def test_cascade_delete_with_shape(self):
        rb = NodeRuleBinding.objects.create(
            shape=self.shape, sop=self.sop, rule_key="step:1:0:1",
        )
        self.assertTrue(NodeRuleBinding.objects.filter(id=rb.id).exists())
        self.shape.delete()
        self.assertFalse(NodeRuleBinding.objects.filter(id=rb.id).exists())


class NodeToolBindingTests(_ShapeFixtureMixin, TestCase):
    """Tools may attach with or without a rule_binding back-pointer."""

    @classmethod
    def setUpTestData(cls):
        cls.shape = cls._make_shape("Tool host")
        cls.sop = _make_audit_sop("SOP-B")
        cls.rb = NodeRuleBinding.objects.create(
            shape=cls.shape, sop=cls.sop, rule_key="step:1:0:0",
        )
        cls.tool = Tool.objects.create(
            name="facets_get_summary_t",
            display_name="Facets",
            kind=Tool.KIND_LANGCHAIN,
        )

    def test_orphan_tool_binding(self):
        binding = NodeToolBinding.objects.create(
            shape=self.shape, tool=self.tool, rule_binding=None,
        )
        self.assertIsNone(binding.rule_binding)

    def test_rule_linked_tool_binding(self):
        binding = NodeToolBinding.objects.create(
            shape=self.shape, tool=self.tool, rule_binding=self.rb,
        )
        self.assertEqual(binding.rule_binding_id, self.rb.id)

    def test_rule_binding_set_null_on_rule_delete(self):
        binding = NodeToolBinding.objects.create(
            shape=self.shape, tool=self.tool, rule_binding=self.rb,
        )
        self.rb.delete()
        binding.refresh_from_db()
        self.assertIsNone(binding.rule_binding)
