"""Loader smoke test: routing metadata flows from the DB into runtime rules.

Verifies the Phase-1 change — ``rule_loader._hydrate_decision`` now carries
``step_number`` / ``is_out_of_scope`` / ``is_final`` / ``aggregation`` /
``applicable_when`` / ``goto_step`` out of the persisted AuditStep/AuditDecision
rows so the step cursor can route on them.
"""
from __future__ import annotations

from django.test import TestCase

from sop_ingestion.models import (AuditDecision, AuditSop, AuditStep,
                                   IngestionJob)
from uhc_execution_engine.rule_loader import _hydrate_decision


class RoutingLoaderTests(TestCase):
    def setUp(self):
        self.job = IngestionJob.objects.create(seed_url="http://example.com/sop")
        self.sop = AuditSop.objects.create(
            job=self.job, title="Smoke SOP", url="http://example.com/sop",
            content_hash="x" * 8)
        self.step = AuditStep.objects.create(
            sop=self.sop, step_number=2, question="Q2", is_out_of_scope=False,
            yaml_rule_id="RULE-002",
        )

    def _dec(self, **kw):
        defaults = dict(
            step=self.step, depth=0, row_index=0, subrule_id="RULE-002-001",
            condition_if="if", action_text="then", decision_type="REFER",
            aggregation="LEAF",
        )
        defaults.update(kw)
        return AuditDecision.objects.create(**defaults)

    def test_goto_and_step_number_carried(self):
        dec = self._dec(goto_step=8)
        out = _hydrate_decision(self.sop, self.step, dec, "", "")
        self.assertEqual(out["step_number"], 2)
        self.assertEqual(out["goto_step"], 8)
        self.assertFalse(out["is_out_of_scope"])
        self.assertEqual(out["aggregation"], "LEAF")
        self.assertEqual(out["applicable_when"], "")

    def test_out_of_scope_from_decision(self):
        dec = self._dec(is_out_of_scope=True, is_final=True, decision_type="STOP")
        out = _hydrate_decision(self.sop, self.step, dec, "", "")
        self.assertTrue(out["is_out_of_scope"])
        self.assertTrue(out["is_final"])

    def test_out_of_scope_inherited_from_step(self):
        # Step marked OOS should propagate even if the decision row isn't.
        self.step.is_out_of_scope = True
        self.step.save(update_fields=["is_out_of_scope"])
        dec = self._dec(is_out_of_scope=False)
        out = _hydrate_decision(self.sop, self.step, dec, "", "")
        self.assertTrue(out["is_out_of_scope"])

    def test_applicable_when_and_aggregation_carried(self):
        dec = self._dec(applicable_when="Provider is individual",
                        aggregation="APPLICABLE_ONLY")
        out = _hydrate_decision(self.sop, self.step, dec, "", "")
        self.assertEqual(out["applicable_when"], "Provider is individual")
        self.assertEqual(out["aggregation"], "APPLICABLE_ONLY")
