"""
End-to-end smoke tests for every LangChain tool in the registry.

Each test invokes a tool through ``POST /api/agent-tools/{name}/invoke``
and asserts the response carries the per-tool shape we promise to the
SPA/agent. Mock upstream endpoints (mounted at ``/api/mocks/``) provide
the underlying HTTP responses, so the whole thing runs in one process
with no external dependencies.

Because the LangChain tools read their upstream URLs from ``os.environ``
at *call* time, we override the relevant env vars in :func:`setUpClass`
to point at the :class:`LiveServerTestCase` URL.
"""
from __future__ import annotations

import os
import unittest
from typing import Any
from unittest import mock

from django.test import LiveServerTestCase, override_settings
from rest_framework.test import APIClient

from agent_tools.models import Tool
from agent_tools.tools._cache import ToolCache
from builder.auth import CorebackendUser


def _client() -> APIClient:
    client = APIClient()
    client.force_authenticate(
        CorebackendUser(id="test-user", email="t@example.com", role="MEMBER"),
    )
    return client


@override_settings(DEBUG=False)
class ToolsEndToEndTests(LiveServerTestCase):
    """One method per tool — each must return ``ok=true`` with a structured result."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        base = cls.live_server_url.rstrip("/") + "/api/mocks"

        # Point every tool's env at the live test server's mock prefix.
        # We use mock.patch.dict so the original env is restored on
        # tearDownClass even if a downstream test mutates os.environ.
        cls._env_patcher = mock.patch.dict(os.environ, {
            "DOC360_API_BASE": f"{base}/doc360",
            "DOC360_TOKEN_URL": f"{base}/doc360/security/tokens",
            "DOC360_READ_DOCUMENT_CONTENT":
                "/api/ecs/doc360-getcontent/v1/document-contents/read",
            "DOC360_CLIENT_ID": "mock",
            "DOC360_CLIENT_SECRET": "mock",
            "FACETS_BASE_URL": f"{base}/facets",
            "FACETS_USERNAME": "mock",
            "FACETS_PASSWORD": "mock",
            "FACET_EXTENSION_PORTAL_BASE_URL": f"{base}/fep",
            "FEP_GROUP_MODEL_BASE_URL": f"{base}/fep/checkModel",
            "CBD_TOKEN_URL": f"{base}/cbd/oauth/token",
            "CBD_API_URL": f"{base}/cbd/coverage",
            "CBD_API": f"{base}/cbd/customer-info",
            "CBD_CLIENT_ID": "mock",
            "CBD_CLIENT_SECRET": "mock",
            "DIAGNOSIS_API_URL": f"{base}/diagnosis",
            "LINX_AUTH_URL": f"{base}/linx/oauth/token",
            "LINX_API_URL": f"{base}/linx/claim-search",
            "CMS_API_BASE_URL": f"{base}/cms",
            "CMS_DATASET_ID": "opt-out-affidavits",
            "NPI_API_BASE_URL": f"{base}/npi/api/",
            "AGENT_TOOLS_SQL_BACKEND": "memory",
            "AGENT_TOOLS_LLM_MOCK": "true",
            "AGENT_TOOLS_LAZY_LOAD": "true",
            "SSL_VERIFY": "false",
        })
        cls._env_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls._env_patcher.stop()
        super().tearDownClass()

    def setUp(self):
        # Tool modules share the process-wide :class:`ToolCache`. Clear it
        # so re-runs don't return stale entries when env vars change.
        ToolCache.clear()

        # LiveServerTestCase extends TransactionTestCase, which flushes
        # the DB between tests. That wipes the rows the seed migration
        # inserted, so we re-seed the registry here. Cheaper than
        # serialized_rollback (which re-dumps every fixture for every
        # test).
        if Tool.objects.count() == 0:
            from agent_tools.registry import sync_to_db
            sync_to_db()

        self.client_ = _client()

    # ── helpers ──────────────────────────────────────────────────────────

    def _invoke(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if not Tool.objects.filter(name=name, is_active=True).exists():
            self.skipTest(f"tool '{name}' not in registry (seed migration skipped?)")
        resp = self.client_.post(
            f"/api/agent-tools/{name}/invoke",
            data={"args": args},
            format="json",
        )
        self.assertEqual(
            resp.status_code, 200,
            f"{name} invoke failed: status={resp.status_code} body={resp.content!r}",
        )
        body = resp.json()
        self.assertTrue(body.get("ok"), f"{name} returned ok=false: {body}")
        self.assertEqual(body.get("tool"), name)
        return body["result"] or {}

    # ── DOC360 ───────────────────────────────────────────────────────────

    def test_doc360_read_claim_by_fln_dcc(self):
        result = self._invoke("doc360_read_claim_by_fln_dcc", {
            "fln_dcc": "1234567890",
        })
        self.assertIsInstance(result, dict)

    # ── Facets ───────────────────────────────────────────────────────────

    def test_facets_get_summary(self):
        result = self._invoke("facets_get_summary", {"claim_number": "25XG44660400"})
        self.assertEqual(result.get("endpoint"), "summary")
        self.assertIn("status_code", result)

    def test_facets_get_cob(self):
        result = self._invoke("facets_get_cob", {"claim_number": "25XG44660400"})
        self.assertEqual(result.get("endpoint"), "cob")

    def test_facets_get_line_details(self):
        result = self._invoke("facets_get_line_details", {"claim_number": "25XG44660400"})
        self.assertEqual(result.get("endpoint"), "line_details")
        self.assertIn("items", result)
        self.assertIsInstance(result["items"], list)

    def test_facets_get_member_eligibility(self):
        result = self._invoke(
            "facets_get_member_eligibility", {"claim_number": "25XG44660400"},
        )
        self.assertIsInstance(result, dict)

    def test_facets_get_provider_details(self):
        result = self._invoke(
            "facets_get_provider_details",
            {"claim_number_for_reference": "25XG44660400"},
        )
        self.assertEqual(result.get("endpoint"), "provider_details")

    def test_facets_get_duplicate_claim(self):
        result = self._invoke(
            "facets_get_duplicate_claim", {"claim_number": "25XG44660400"},
        )
        self.assertEqual(result.get("endpoint"), "duplicate_claim")

    # ── Facet Extension Portal ───────────────────────────────────────────

    def test_facet_extension_portal_provider(self):
        result = self._invoke(
            "facet_extension_portal_provider", {"provider_id": "FAC000022500"},
        )
        self.assertIsInstance(result, dict)

    def test_facet_extension_portal_programme(self):
        result = self._invoke(
            "facet_extension_portal_programme", {"program_detailed_id": "276728"},
        )
        self.assertIsInstance(result, dict)

    def test_facet_ext_portal_group_model(self):
        result = self._invoke(
            "facet_ext_portal_group_model", {"claim_number": "25XG44660400"},
        )
        self.assertIsInstance(result, dict)

    # ── CBD ──────────────────────────────────────────────────────────────

    def test_check_medicare_coverage(self):
        result = self._invoke("check_medicare_coverage", {
            "cpt_codes": ["99213"],
            "group_name": "Standard Medicare",
            "plan_name": "Standard Medicare",
        })
        self.assertIsInstance(result, dict)

    # ── Diagnosis ────────────────────────────────────────────────────────

    def test_check_diagnosis_coverage(self):
        result = self._invoke("check_diagnosis_coverage", {"diagnosis_code": "I10"})
        self.assertIsInstance(result, dict)

    # ── LINX ─────────────────────────────────────────────────────────────

    def test_linx_claim_search(self):
        result = self._invoke("linx_claim_search", {"subscriber_id": "123456789"})
        self.assertIsInstance(result, dict)

    # ── Opt-out ──────────────────────────────────────────────────────────

    def test_medicare_optout_checker(self):
        # The upstream tool returns a JSON-encoded **string** (per the
        # tool docstring); the LangGraph wrapper preserves the return
        # value verbatim so we re-parse here.
        import json as _json
        raw = self._invoke("medicare_optout_checker", {
            "last_name": "DOE",
            "state": "MA",
        })
        self.assertIsInstance(raw, str)
        parsed = _json.loads(raw)
        # Either a list of formatted records or {"message": "..."} when
        # the mock CMS dataset has no matches.
        self.assertTrue(isinstance(parsed, (list, dict)))

    # ── Cross-prevalence ─────────────────────────────────────────────────

    def test_check_cross_prevalence_billing(self):
        result = self._invoke("check_cross_prevalence_billing", {
            "cpt_code_a": "99213",
            "cpt_code_b": "G0548",
        })
        self.assertIsInstance(result, dict)

    # ── SOP step persistence (in-memory backend) ─────────────────────────

    def test_save_sop_step(self):
        result = self._invoke("save_sop_step", {
            "execution_id": "exec-1",
            "claim_id": "25XG44660400",
            "agent_name": "test-agent",
            "sop_name": "DUP",
            "sop_step_number": 1,
            "step_exec_status": "SUCCESS",
        })
        self.assertIsInstance(result, dict)

    # ── LLM (mock mode) ──────────────────────────────────────────────────

    def test_llm_parse_claim_with_ontology(self):
        result = self._invoke("llm_parse_claim_with_ontology", {
            "claim_data": {"content": "TOTAL CHARGE $123.45"},
        })
        self.assertIsInstance(result, dict)

    # ── Electronic / deterministic parser ────────────────────────────────

    def test_claim_parse_flat_template_with_confidence(self):
        result = self._invoke("claim_parse_flat_template_with_confidence", {
            "claim_data": {"content": "Box 21: 1 I10\nTOTAL CHARGE $123.45"},
        })
        self.assertIsInstance(result, dict)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
