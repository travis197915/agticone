"""Canvas-authored custom rules (and manual sub-rules) must materialise into
executable rules via ``rule_loader.load_workflow_bindings``.

Custom rules live only in ``Shape.properties.sop_rules`` (key ``custom:...``),
never as NodeRuleBinding rows. The loader must surface them as decision rules,
grouped on their shape, inheriting a host (sop_id, step_number) so the execution
engine evaluates them like SOP rules — and carry ``parent_key`` / ``depth`` so a
sub-rule nests under its parent.
"""
from __future__ import annotations

from django.test import TestCase

from builder.models import (Shape, ShapeCategory, ShapeDefinition, WorkArea,
                            Workbench, Workflow)
from uhc_execution_engine.rule_loader import load_workflow_bindings


class CustomRuleLoaderTests(TestCase):
    def setUp(self):
        self.wf = Workflow.objects.create(name="WF", slug="wf-custom-rules")
        area = WorkArea.objects.create(workflow=self.wf, name="A", order=0)
        self.bench = Workbench.objects.create(work_area=area, name="B", order=0)
        cat = ShapeCategory.objects.create(slug="flow", label="Flow")
        self.defn = ShapeDefinition.objects.create(
            category=cat, slug="rectangle", label="Rectangle", kind="rectangle")

    def _shape(self, sop_rules, **props):
        return Shape.objects.create(
            workbench=self.bench, definition=self.defn, label="Node",
            properties={"sop_rules": sop_rules, **props})

    def test_custom_rule_and_subrule_materialise(self):
        self._shape(
            [
                {
                    "key": "custom:parent", "is_custom": True, "depth": 0,
                    "decision_type": "REFER", "condition": "if A", "action": "do X",
                    "section_label": "Manual rule", "codes": ["EX1"],
                },
                {
                    "key": "custom:child", "is_custom": True, "depth": 1,
                    "parent_key": "custom:parent",
                    "decision_type": "DENY", "condition": "if B", "action": "do Y",
                    "section_label": "Manual sub-rule",
                },
            ],
            sop_id=42, step_number=3,
        )

        out = load_workflow_bindings(str(self.wf.id))
        decisions = {r["key"]: r for r in out["decisions"]}

        self.assertIn("custom:parent", decisions)
        self.assertIn("custom:child", decisions)

        parent = decisions["custom:parent"]
        child = decisions["custom:child"]
        # Host (sop_id, step_number) inherited from the shape props.
        self.assertEqual(parent["sop_id"], 42)
        self.assertEqual(parent["step_number"], 3)
        self.assertEqual(child["step_number"], 3)
        # Nesting + content preserved, ready for the evaluator.
        self.assertEqual(child["parent_key"], "custom:parent")
        self.assertEqual(child["depth"], 1)
        self.assertEqual(child["decision_type"], "DENY")
        self.assertEqual(parent["codes"], ["EX1"])
        self.assertTrue(parent["is_custom"])

        # Both land on the same shape group so they execute on one node.
        shapes = out["shapes"]
        self.assertEqual(len(shapes), 1)
        keys_on_shape = {r["key"] for r in shapes[0]["rules"]}
        self.assertEqual(keys_on_shape, {"custom:parent", "custom:child"})

    def test_manual_oos_propagates_to_custom_rules(self):
        self._shape(
            [{"key": "custom:r1", "is_custom": True, "decision_type": "NOTE",
              "condition": "", "action": "note"}],
            manual_out_of_scope=True,
        )
        out = load_workflow_bindings(str(self.wf.id))
        r1 = next(r for r in out["decisions"] if r["key"] == "custom:r1")
        self.assertTrue(r1["manual_oos"])
        # No SOP host → parked at a synthetic step so it still executes.
        self.assertIsNotNone(r1["step_number"])
