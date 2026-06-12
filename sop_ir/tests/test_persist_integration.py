"""DB-side projection test for the shared persist gate.

Proves that ``persist_ir`` writes a routing-complete projection — the exact rows
the execution engine's step cursor reads — for the three routing patterns:

  * static goto  (skip to step N)
  * applicable_only branches (applicable_when -> APPLICABLE_ONLY)
  * out-of-scope terminal exclusion

and that it records the authoritative ``SopIRDocument`` control-plane head.

SAFETY: this is a Django ``TransactionTestCase`` so it ONLY ever runs against
the ephemeral test database created/destroyed by the Django test runner. Run::

    PYTHONPATH=. python manage.py test sop_ir.tests.test_persist_integration

It is intentionally NOT collected by plain ``pytest`` (which would hit the
configured live DB) — run it through ``manage.py test`` only.
"""
from __future__ import annotations

import os
import sys
import unittest

_RUN = os.environ.get("RUN_DJANGO_DB_TESTS") == "1" or "test" in sys.argv

if _RUN:
    from django.test import TransactionTestCase

    from sop_ingestion.models import (AuditDecision, AuditSop, AuditStep,
                                       IngestionJob, SopIRDocument)
    from sop_ir.persist import persist_ir
    from sop_ir.schema import SopIR

    class PersistIRProjectionTest(TransactionTestCase):
        def _make_sop(self) -> AuditSop:
            job = IngestionJob.objects.create(seed_url="http://example.test/sop")
            return AuditSop.objects.create(
                job=job, url="http://example.test/sop", content_hash="hash-1",
                title="Parity Demo SOP",
            )

        def _ir(self) -> SopIR:
            return SopIR.model_validate({
                "metadata": {"document_title": "Parity Demo SOP"},
                "rules": [
                    {"rule_id": "RULE-001", "step_number": 1,
                     "description": "Check provider type",
                     "subrules": [
                         {"subrule_id": "RULE-001-001",
                          "description": "Provider is individual",
                          "applicable_when": "Provider is individual",
                          "actions": ["match names; skip to step 3"]},
                         {"subrule_id": "RULE-001-002",
                          "description": "Provider is group",
                          "applicable_when": "Provider is group",
                          "actions": ["match org name"]},
                     ]},
                    {"rule_id": "RULE-002", "step_number": 2,
                     "description": "Dental exclusion",
                     "subrules": [
                         {"subrule_id": "RULE-002-001",
                          "description": "line is dental",
                          "actions": ["stop further auditing as this is out of scope"]},
                     ]},
                    {"rule_id": "RULE-003", "step_number": 3,
                     "description": "Final",
                     "actions": ["Process the claim (F3)"]},
                ],
            })

        def test_projection_is_routing_complete(self):
            sop = self._make_sop()
            ir = self._ir()
            stats = persist_ir(sop, ir, job=sop.job, source="test",
                               validation={"ok": True, "errors": []})

            self.assertEqual(stats["steps"], 3)
            self.assertGreaterEqual(stats["decisions"], 4)

            # applicable_only branch with goto
            s1 = AuditStep.objects.get(sop=sop, step_number=1)
            d1 = AuditDecision.objects.get(step=s1, subrule_id="RULE-001-001")
            self.assertEqual(d1.aggregation, "APPLICABLE_ONLY")
            self.assertEqual(d1.applicable_when, "Provider is individual")
            self.assertEqual(d1.goto_step, 3)

            # out-of-scope terminal exclusion
            s2 = AuditStep.objects.get(sop=sop, step_number=2)
            d2 = AuditDecision.objects.get(step=s2, subrule_id="RULE-002-001")
            self.assertTrue(d2.is_out_of_scope)
            self.assertTrue(d2.is_final)

            # SopIRDocument control-plane head
            doc = SopIRDocument.objects.get(sop=sop)
            self.assertEqual(doc.validation_status, "OK")
            self.assertEqual(doc.rule_count, 3)
            self.assertEqual(doc.ir_version, 1)

        def test_reimport_is_idempotent_and_versions(self):
            sop = self._make_sop()
            ir = self._ir()
            persist_ir(sop, ir, job=sop.job, source="test")
            first = AuditDecision.objects.filter(step__sop=sop).count()
            persist_ir(sop, ir, job=sop.job, source="test")
            second = AuditDecision.objects.filter(step__sop=sop).count()
            self.assertEqual(first, second)  # wipe+rebuild, no duplication
            self.assertEqual(SopIRDocument.objects.filter(sop=sop).count(), 2)
            self.assertEqual(
                SopIRDocument.objects.filter(sop=sop).order_by("-ir_version")
                .first().ir_version, 2)
else:  # pragma: no cover
    class _Skipped(unittest.TestCase):
        @unittest.skip("DB integration test — run via `manage.py test "
                       "sop_ir.tests.test_persist_integration`")
        def test_placeholder(self):
            pass
