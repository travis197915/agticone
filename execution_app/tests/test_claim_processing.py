from __future__ import annotations

from datetime import timedelta
from unittest import mock
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from agent_tools.models import NodeRuleBinding, NodeToolBinding, Tool
from builder.models import Shape, ShapeCategory, ShapeDefinition, WorkArea, Workbench, Workflow
from execution_app.models import BatchExecutionRun, RuleEvaluation, RuleExecutionRun, ToolInvocationRecord
from sop_ingestion.models import AuditSop, IngestionJob


class ClaimProcessingEndpointTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.workflow = Workflow.objects.create(name="WF", slug=f"wf-{uuid.uuid4().hex[:8]}")
        self.batch = BatchExecutionRun.objects.create(
            workflow=self.workflow,
            source_filename="claims.xlsx",
            claim_id_column="claim_id",
            total_claims=1,
            status="COMPLETED",
        )

        category = ShapeCategory.objects.create(slug=f"cat-{uuid.uuid4().hex[:8]}", label="General")
        definition = ShapeDefinition.objects.create(
            category=category,
            slug=f"shape-def-{uuid.uuid4().hex[:8]}",
            label="Xmed Diagnosis Coverage",
            kind="rectangle",
        )
        area = WorkArea.objects.create(workflow=self.workflow, name="Area 1")
        bench = Workbench.objects.create(work_area=area, name="Bench 1")
        self.shape = Shape.objects.create(workbench=bench, definition=definition, label="Xmed Diagnosis Coverage")

        self.ingestion_job = IngestionJob.objects.create(seed_url="https://example.com/sop")
        self.sop = AuditSop.objects.create(
            job=self.ingestion_job,
            url="https://example.com/sop/doc",
            content_hash=f"hash-{uuid.uuid4().hex}",
        )
        self.tool = Tool.objects.create(
            name=f"tool-{uuid.uuid4().hex[:8]}",
            display_name="Facets Get Line Details",
            invoke_url="/api/agent-tools/facets_get_line_details/invoke",
        )
        self.rule_binding = NodeRuleBinding.objects.create(
            shape=self.shape,
            sop=self.sop,
            rule_key=f"rule-{uuid.uuid4().hex[:6]}",
            condition="Service must be covered",
            action="Mark as covered",
        )
        self.tool_binding = NodeToolBinding.objects.create(shape=self.shape, tool=self.tool)

    def _create_run(self, *, claim_id: str, status: str = "COMPLETED") -> RuleExecutionRun:
        started = timezone.now() - timedelta(minutes=10)
        finished = timezone.now() - timedelta(minutes=7)
        run = RuleExecutionRun.objects.create(
            batch=self.batch,
            workflow=self.workflow,
            claim_id=claim_id,
            status=status,
            started_at=started,
            finished_at=finished,
            final_decision_type="APPROVE",
            narrative="Narrative text",
        )
        RuleEvaluation.objects.create(
            run=run,
            order_index=1,
            rule_binding=self.rule_binding,
            rule_key=self.rule_binding.rule_key,
            rule_source="DECISION",
            condition="Type of service is valid",
            action="Proceed",
            matched=True,
            confidence=0.98,
            reasoning="Coverage requirement matched against configured decision rule.",
            decision_type="APPROVE",
        )
        ToolInvocationRecord.objects.create(
            run=run,
            tool_binding=self.tool_binding,
            tool_name="facets_get_line_details",
            phase="EVALUATE",
            ok=True,
            duration_ms=12000,
        )
        ToolInvocationRecord.objects.create(
            run=run,
            tool_name="facets_get_line_details",
            phase="FETCH",
            ok=True,
            duration_ms=1234,
        )
        return run

    def test_happy_path_returns_aggregate_contract(self):
        run = self._create_run(claim_id="25XH48861400")
        resp = self.client.get("/api/claims/25XH48861400/processing/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertEqual(body["claimId"], "25XH48861400")
        self.assertEqual(body["runId"], str(run.id))
        self.assertEqual(body["batchId"], str(self.batch.id))
        self.assertEqual(body["workflowId"], str(self.workflow.id))
        self.assertEqual(body["claimStatus"], "CLEAN")
        self.assertEqual(body["reviewStatus"], None)
        self.assertEqual(body["feedback"], None)
        self.assertEqual(len(body["agents"]), 1)
        self.assertEqual(body["agents"][0]["status"], "CLEAN")
        self.assertEqual(body["agents"][0]["steps"][0]["duration"], "12s")
        self.assertEqual(len(body["outerToolInvocations"]), 1)
        self.assertEqual(body["outerToolInvocations"][0]["phase"], "FETCH")

    def test_run_id_overrides_claim_lookup(self):
        run = self._create_run(claim_id="RUN-ID-CLAIM")
        resp = self.client.get(f"/api/claims/DIFFERENT/processing/?run_id={run.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["claimId"], "RUN-ID-CLAIM")

    def test_missing_claim_returns_404(self):
        resp = self.client.get("/api/claims/UNKNOWN-CLAIM/processing/")
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertEqual(body["source"], "django")
        self.assertIn("No run found for claim UNKNOWN-CLAIM", body["error"])

    def test_bad_uuid_query_param_returns_400(self):
        resp = self.client.get("/api/claims/25XH48861400/processing/?run_id=not-a-uuid")
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["source"], "django")
        self.assertIn("Malformed run_id", body["error"])

    def test_running_claim_reports_in_progress_status(self):
        run = RuleExecutionRun.objects.create(
            batch=self.batch,
            workflow=self.workflow,
            claim_id="RUNNING-CLAIM",
            status="RUNNING",
        )
        resp = self.client.get("/api/claims/RUNNING-CLAIM/summary/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["runStatus"], "RUNNING")
        self.assertEqual(body["claimStatus"], "IN_PROGRESS")

    def test_patch_review_status_in_progress(self):
        run = self._create_run(claim_id="REVIEW-CLAIM")
        resp = self.client.patch(
            f"/api/execute/runs/{run.id}/review-status/",
            {"reviewStatus": "in_progress"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["reviewStatus"], "in_progress")
        run.refresh_from_db()
        self.assertEqual(run.review_status, "in_progress")

        summary = self.client.get("/api/claims/REVIEW-CLAIM/summary/")
        self.assertEqual(summary.json()["reviewStatus"], "in_progress")

    def test_patch_claim_review_status_endpoint(self):
        run = self._create_run(claim_id="REVIEW-CLAIM-2")
        resp = self.client.patch(
            "/api/claims/REVIEW-CLAIM-2/review-status/",
            {"review_status": "in_progress"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["runId"], str(run.id))
        self.assertEqual(resp.json()["reviewStatus"], "in_progress")


class PersistFailureRegressionTests(TestCase):
    """If RuleEvaluation.bulk_create raises, the parent RuleExecutionRun
    must end up FAILED with the real error — not COMPLETED with zero
    children (the bug that hid persistence failures from operators)."""

    def setUp(self) -> None:
        self.workflow = Workflow.objects.create(
            name="WF", slug=f"wf-{uuid.uuid4().hex[:8]}")
        # Mimic what n01_validate._reserve_run_row does at the start of
        # the engine: a RUNNING parent row with this run_id.
        self.run_id = uuid.uuid4()
        self.run = RuleExecutionRun.objects.create(
            id=self.run_id,
            workflow=self.workflow,
            claim_id="CLAIM-1",
            status="RUNNING",
        )

    def _state(self) -> dict:
        return {
            "run_id": str(self.run_id),
            "workflow_id": str(self.workflow.id),
            "claim_id": "CLAIM-1",
            "claim": {"claim_id": "CLAIM-1"},
            "raw_fetch": {},
            "status": "COMPLETED",
            "final_decision_type": "ALLOW",
            "applied_codes": [],
            "narrative": "ok",
            "stages": [],
            "tool_invocations": [],
            "rule_results": [{
                "order_index": 0,
                "shape_id": "s1",
                "shape_label": "Shape 1",
                "rule_key": "step:1:1:0",
                "binding_id": "",
                "source": "decision",
                "condition": "x",
                "action": "y",
                "matched": True,
                "confidence": 0.9,
                "reasoning": "because",
                "decision_type": "ALLOW",
                "codes": [],
                "tool_results_used": [],
                "llm_provider": "anthropic",
                "llm_ms": 12,
            }],
        }

    def test_child_failure_rolls_back_parent_and_records_error(self):
        from uhc_execution_engine.agents.n07_persist_respond import (
            persist_and_respond,
        )

        with mock.patch(
            "execution_app.models.RuleEvaluation.objects.bulk_create",
            side_effect=ValueError("boom"),
        ):
            result = persist_and_respond(self._state())

        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["error_message"].startswith("persist: "))
        self.assertIn("boom", result["error_message"])

        # Atomic rollback: no half-written children.
        self.assertEqual(
            RuleEvaluation.objects.filter(run=self.run).count(), 0)

        # Recovery update: parent row now reflects the real failure.
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "FAILED")
        self.assertIn("boom", self.run.error_message)
        self.assertIsNotNone(self.run.finished_at)

    def test_successful_persist_writes_children(self):
        """Sanity check — the atomic block doesn't break the happy path."""
        from uhc_execution_engine.agents.n07_persist_respond import (
            persist_and_respond,
        )

        result = persist_and_respond(self._state())

        self.assertNotIn("error_message", result.get("response", {})) or \
            self.assertEqual(result["response"]["error_message"], "")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "COMPLETED")
        self.assertEqual(
            RuleEvaluation.objects.filter(run=self.run).count(), 1)
