"""
Round-trip tests for the builder graph endpoint with DB-backed bindings.

The plan deliberately keeps ``Shape.properties.{sop_rules, tool_calls}``
as a thin back-compat envelope while moving the source of truth into
:class:`NodeRuleBinding` and :class:`NodeToolBinding`. These tests
verify the two halves of that bargain:

* PUT a graph with one rule + one tool picked alongside that rule →
  rows land in the binding tables and the tool's ``rule_binding`` FK
  points at the right rule.
* GET the same graph → ``properties`` is hydrated from the binding
  tables, including the ``rule_binding_id`` + ``rule_key`` fields the
  SPA's grouping logic uses.

We don't exercise the full canvas write path here; only the per-shape
``extract_bindings_from_properties`` / ``hydrate_properties_with_bindings``
contract. That keeps the tests fast and avoids needing a fully wired
SOP-ingestion pipeline.
"""
from __future__ import annotations

import uuid

from django.test import TestCase

from agent_tools.models import NodeRuleBinding, NodeToolBinding, Tool
from builder.bindings_sync import (
    extract_bindings_from_properties,
    hydrate_properties_with_bindings,
)
from builder.models import (
    Shape, ShapeCategory, ShapeDefinition, WorkArea, Workbench, Workflow,
)
from sop_ingestion.models import AuditSop, IngestionJob


def _make_shape() -> Shape:
    wf = Workflow.objects.create(
        name=f"WF-{uuid.uuid4().hex[:6]}",
        slug=f"wf-{uuid.uuid4().hex[:6]}",
        description="",
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
    return Shape.objects.create(workbench=wb, definition=sd, label="N")


def _make_sop() -> AuditSop:
    job = IngestionJob.objects.create(
        seed_url="https://example.com/sop.html", status="COMPLETED",
    )
    return AuditSop.objects.create(
        job=job,
        title="SOP",
        url="https://example.com/sop.html",
        content_hash=uuid.uuid4().hex,
        doc_format="HTML",
    )


class BindingExtractionTests(TestCase):
    """``Shape.properties`` → binding tables."""

    @classmethod
    def setUpTestData(cls):
        cls.tool, _ = Tool.objects.get_or_create(
            name="facets_get_summary",
            defaults={
                "display_name": "Facets Summary",
                "kind": Tool.KIND_LANGCHAIN,
                "invoke_url": "/api/agent-tools/facets_get_summary/invoke",
            },
        )

    def test_extract_creates_rule_and_tool_with_linkage(self):
        shape = _make_shape()
        sop = _make_sop()
        rule_key = f"step:{sop.id}:1:0"

        shape.properties = {
            "sop_rules": [{
                "key":            rule_key,
                "sop_id":         sop.id,
                "condition":      "DOS > 90 days",
                "action":         "Deny — timely filing",
                "references":     [],
                "excluded_by":    [],
                "html_reference": {"anchor": "step_1"},
            }],
            "tool_calls": [{
                "tool_id":   str(self.tool.id),
                "name":      self.tool.name,
                "rule_key":  rule_key,
            }],
        }
        shape.save(update_fields=["properties"])

        extract_bindings_from_properties(shape)

        rule_rows = list(NodeRuleBinding.objects.filter(shape=shape))
        self.assertEqual(len(rule_rows), 1)
        self.assertEqual(rule_rows[0].rule_key, rule_key)
        self.assertEqual(rule_rows[0].condition, "DOS > 90 days")

        tool_rows = list(NodeToolBinding.objects.filter(shape=shape))
        self.assertEqual(len(tool_rows), 1)
        self.assertEqual(tool_rows[0].tool_id, self.tool.id)
        self.assertEqual(tool_rows[0].rule_binding_id, rule_rows[0].id)

    def test_extract_is_wipe_and_reinsert(self):
        """Re-PUTting the same shape replaces existing bindings."""
        shape = _make_shape()
        sop = _make_sop()
        shape.properties = {
            "sop_rules": [{
                "key": f"step:{sop.id}:1:0", "sop_id": sop.id,
                "condition": "first", "action": "a",
            }],
            "tool_calls": [],
        }
        shape.save(update_fields=["properties"])
        extract_bindings_from_properties(shape)
        self.assertEqual(NodeRuleBinding.objects.filter(shape=shape).count(), 1)

        shape.properties = {
            "sop_rules": [{
                "key": f"step:{sop.id}:2:0", "sop_id": sop.id,
                "condition": "second", "action": "b",
            }],
            "tool_calls": [],
        }
        shape.save(update_fields=["properties"])
        extract_bindings_from_properties(shape)
        rows = list(NodeRuleBinding.objects.filter(shape=shape))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].rule_key, f"step:{sop.id}:2:0")


class BindingHydrationTests(TestCase):
    """Binding tables → ``Shape.properties`` envelope."""

    @classmethod
    def setUpTestData(cls):
        cls.tool, _ = Tool.objects.get_or_create(
            name="facets_get_summary",
            defaults={
                "display_name": "Facets Summary",
                "kind": Tool.KIND_LANGCHAIN,
                "invoke_url": "/api/agent-tools/facets_get_summary/invoke",
            },
        )

    def test_hydrate_envelope_carries_rule_link(self):
        shape = _make_shape()
        sop = _make_sop()
        rb = NodeRuleBinding.objects.create(
            shape=shape, sop=sop,
            rule_key="step:1:0:0",
            condition="A",
            action="B",
        )
        tb = NodeToolBinding.objects.create(
            shape=shape, tool=self.tool, rule_binding=rb,
        )

        out = hydrate_properties_with_bindings(shape)
        self.assertIn("sop_rules", out)
        self.assertIn("tool_calls", out)

        self.assertEqual(len(out["sop_rules"]), 1)
        self.assertEqual(out["sop_rules"][0]["key"], "step:1:0:0")
        self.assertEqual(out["sop_rules"][0]["condition"], "A")

        self.assertEqual(len(out["tool_calls"]), 1)
        tc = out["tool_calls"][0]
        self.assertEqual(tc["name"], "facets_get_summary")
        self.assertEqual(tc["tool_kind"], "langchain")
        self.assertEqual(tc["rule_binding_id"], str(rb.id))
        self.assertEqual(tc["rule_key"], "step:1:0:0")
        self.assertEqual(tc["id"], str(tb.id))


class RoundTripTests(TestCase):
    """PUT → GET → PUT preserves the envelope shape."""

    def test_round_trip_preserves_rule_and_tool(self):
        tool, _ = Tool.objects.get_or_create(
            name="facets_get_cob",
            defaults={
                "display_name": "Facets COB",
                "kind": Tool.KIND_LANGCHAIN,
                "invoke_url": "/api/agent-tools/facets_get_cob/invoke",
            },
        )
        shape = _make_shape()
        sop = _make_sop()
        rule_key = f"pre:{sop.id}:7:0"

        # PUT 1 — write JSON blobs and project them.
        shape.properties = {
            "sop_rules": [{
                "key":       rule_key, "sop_id": sop.id,
                "condition": "Eligible", "action": "Allow",
            }],
            "tool_calls": [{
                "tool_id":  str(tool.id), "name": tool.name,
                "rule_key": rule_key,
            }],
        }
        shape.save(update_fields=["properties"])
        extract_bindings_from_properties(shape)

        # GET — hydrate from DB.
        hydrated = hydrate_properties_with_bindings(shape)
        self.assertEqual(len(hydrated["sop_rules"]), 1)
        self.assertEqual(len(hydrated["tool_calls"]), 1)
        rule_binding_id = hydrated["tool_calls"][0]["rule_binding_id"]
        self.assertIsNotNone(rule_binding_id)

        # PUT 2 — write the hydrated envelope back as-is, mimicking the
        # SPA round-trip.
        shape.properties = {
            "sop_rules":  hydrated["sop_rules"],
            "tool_calls": hydrated["tool_calls"],
        }
        shape.save(update_fields=["properties"])
        extract_bindings_from_properties(shape)

        again = hydrate_properties_with_bindings(shape)
        self.assertEqual(len(again["sop_rules"]), 1)
        self.assertEqual(len(again["tool_calls"]), 1)
        self.assertEqual(again["tool_calls"][0]["rule_key"], rule_key)
        self.assertEqual(again["sop_rules"][0]["key"], rule_key)
