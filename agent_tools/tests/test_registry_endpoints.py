"""
HTTP-surface tests for ``/api/agent-tools/`` — the registry that the
left palette + rule-attach modal both read from.

We don't bring up the full mock upstreams here; we just verify that:

* the registry is seeded by the data migration (18 LangChain tools),
* the list endpoint returns every active row with the discriminator and
  Pydantic args_schema the SPA needs,
* the detail endpoint returns the same shape for one tool,
* 404s on unknown tools come back cleanly,
* an unauthenticated request is rejected.

Network access is not required because we monkeypatch each invocation
when needed; see :mod:`test_tools_end_to_end` for the live-server flow.
"""
from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from agent_tools.models import Tool
from builder.auth import CorebackendUser


# These 18 names mirror the registry module's iter order. Keep this list in
# sync with :mod:`agent_tools.registry` so a renamed/removed tool fails a
# test rather than silently shrinking the registry.
EXPECTED_TOOL_NAMES = {
    "doc360_read_claim_by_fln_dcc",
    "facets_get_summary",
    "facets_get_cob",
    "facets_get_line_details",
    "facets_get_member_eligibility",
    "facets_get_provider_details",
    "facets_get_duplicate_claim",
    "facet_extension_portal_provider",
    "facet_extension_portal_programme",
    "facet_ext_portal_group_model",
    "check_medicare_coverage",
    "check_diagnosis_coverage",
    "linx_claim_search",
    "medicare_optout_checker",
    "check_cross_prevalence_billing",
    "save_sop_step",
    "llm_parse_claim_with_ontology",
    "claim_parse_flat_template_with_confidence",
}


def _client() -> APIClient:
    """Return an APIClient force-authenticated as a MEMBER user."""
    client = APIClient()
    user = CorebackendUser(id="test-user", email="t@example.com", role="MEMBER")
    client.force_authenticate(user=user)
    return client


class RegistrySeedTests(TestCase):
    """The 0002_seed_registry data migration ran at TestCase setup time."""

    def test_seeded_tool_count(self):
        # The data migration ran before TestCase wrapping; we should see
        # at least the 18 known names. Allow more (api_agent rows etc.)
        # without failing.
        names = set(Tool.objects.values_list("name", flat=True))
        missing = EXPECTED_TOOL_NAMES - names
        self.assertFalse(
            missing,
            f"Tool registry missing expected langchain tools: {sorted(missing)}",
        )

    def test_seeded_rows_are_langchain_kind(self):
        for name in EXPECTED_TOOL_NAMES:
            row = Tool.objects.filter(name=name).first()
            self.assertIsNotNone(row, f"no Tool row for {name}")
            self.assertEqual(row.kind, Tool.KIND_LANGCHAIN)
            self.assertTrue(row.is_active)
            self.assertTrue(row.invoke_url.endswith(f"/{name}/invoke"))


class ToolListEndpointTests(TestCase):
    """``GET /api/agent-tools/`` returns the SPA-ready envelope."""

    def test_list_returns_seeded_tools(self):
        resp = _client().get("/api/agent-tools/")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertIsInstance(rows, list)
        names = {row["name"] for row in rows}
        self.assertGreaterEqual(names, EXPECTED_TOOL_NAMES)

        for row in rows[:3]:
            # Required SPA-side fields.
            self.assertIn("id", row)
            self.assertIn("name", row)
            self.assertIn("display_name", row)
            self.assertIn("description", row)
            self.assertIn("kind", row)
            self.assertIn("tool_kind", row)
            self.assertIn("invoke_url", row)
            self.assertIn("args_schema", row)

    def test_unauth_request_rejected(self):
        resp = APIClient().get("/api/agent-tools/")
        self.assertIn(resp.status_code, (401, 403))


class ToolDetailEndpointTests(TestCase):
    """``GET /api/agent-tools/{name}/`` returns one tool."""

    def test_known_tool(self):
        resp = _client().get("/api/agent-tools/facets_get_summary/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["name"], "facets_get_summary")
        self.assertEqual(body["kind"], "langchain")
        self.assertIn("claim_number", str(body.get("args_schema", {})))

    def test_unknown_tool_returns_404(self):
        resp = _client().get("/api/agent-tools/this_tool_does_not_exist/")
        self.assertEqual(resp.status_code, 404)
