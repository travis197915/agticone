"""
ACL enforcement tests for HasPermission (builder/auth.py).

Mirrors Node's requirePermission/mountAccess parity tests
(uhc-claims-backend/tests/smoke.test.ts) on the Django side — these are the
views the audit-review-dashboard calls directly, bypassing Node's proxy
entirely, so this is the *only* enforcement that runs for them.
"""
from __future__ import annotations

import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from builder.auth import CorebackendUser


def _auth(client: APIClient, permissions: list[str]) -> None:
    client.force_authenticate(
        user=CorebackendUser(id="u", email="u@example.com", role="TEST", permissions=permissions),
    )


class ClaimReviewPermissionTests(TestCase):
    """Uses a nonexistent claim_id — a 404 proves the permission check passed
    and the view reached its lookup; a 403 proves it didn't."""

    def setUp(self) -> None:
        self.client = APIClient()
        self.claim_url = "/api/claims/does-not-exist"

    def test_no_permissions_blocked_everywhere(self) -> None:
        _auth(self.client, [])
        self.assertEqual(self.client.get(f"{self.claim_url}/summary/").status_code, 403)
        self.assertEqual(self.client.post(f"{self.claim_url}/review/approve/").status_code, 403)

    def test_claims_read_grants_summary_not_writes(self) -> None:
        _auth(self.client, ["claims:read"])
        self.assertNotEqual(self.client.get(f"{self.claim_url}/summary/").status_code, 403)
        self.assertEqual(self.client.post(f"{self.claim_url}/review/approve/").status_code, 403)

    def test_auditor_grant_set_matches_seeded_role(self) -> None:
        # Full AUDITOR grant set (prisma/migrations/20260720093000_... plus
        # the 20260720101500 correction). Release was added after the audit
        # dashboard's claim detail page turned out to gate it by claim-lock
        # ownership (isCurrentHolder), not role — auditors releasing a claim
        # they're reviewing is core workflow, not an admin action.
        _auth(self.client, [
            "claims:read", "claims:review-status:update",
            "claims:review:approve", "claims:review:reject", "claims:review:release",
        ])
        self.assertNotEqual(self.client.get(f"{self.claim_url}/summary/").status_code, 403)
        self.assertNotEqual(self.client.post(f"{self.claim_url}/review/approve/").status_code, 403)
        self.assertNotEqual(self.client.post(f"{self.claim_url}/review/reject/").status_code, 403)
        self.assertNotEqual(self.client.post(f"{self.claim_url}/review/release/").status_code, 403)

    def test_claims_review_release_not_implied_by_approve_reject(self) -> None:
        _auth(self.client, ["claims:review:approve", "claims:review:reject"])
        self.assertEqual(self.client.post(f"{self.claim_url}/review/release/").status_code, 403)

    def test_wildcard_passes_everything(self) -> None:
        _auth(self.client, ["*"])
        self.assertNotEqual(self.client.get(f"{self.claim_url}/summary/").status_code, 403)
        self.assertNotEqual(self.client.post(f"{self.claim_url}/review/approve/").status_code, 403)
        self.assertNotEqual(self.client.post(f"{self.claim_url}/review/release/").status_code, 403)

    def test_unauthenticated_is_401_not_403(self) -> None:
        self.assertEqual(self.client.get(f"{self.claim_url}/summary/").status_code, 401)


class ExecutionPermissionTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.workflow_id = uuid.uuid4()

    def test_execution_read_does_not_grant_run_batch(self) -> None:
        _auth(self.client, ["execution:read"])
        self.assertNotEqual(self.client.get("/api/execute/runs/").status_code, 403)
        self.assertEqual(
            self.client.post(f"/api/execute/workflows/{self.workflow_id}/run-batch/").status_code,
            403,
        )

    def test_execution_manage_grants_run_batch(self) -> None:
        _auth(self.client, ["execution:manage"])
        # No file attached — will fail validation, but must get *past* the
        # permission check first (i.e. not 403).
        self.assertNotEqual(
            self.client.post(f"/api/execute/workflows/{self.workflow_id}/run-batch/").status_code,
            403,
        )


class BuilderWorkflowPermissionTests(TestCase):
    """Matches the original requireBuilderAccess exactly: AUDITOR may only
    list workflows — every other action, including detail GET, is blocked."""

    def setUp(self) -> None:
        self.client = APIClient()

    def test_workflows_list_permission_allows_list_only(self) -> None:
        _auth(self.client, ["workflows:list"])
        self.assertEqual(self.client.get("/api/builder/workflows/").status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/builder/workflows/{uuid.uuid4()}/").status_code, 403,
        )
        self.assertEqual(self.client.post("/api/builder/workflows/", {}).status_code, 403)

    def test_builder_manage_allows_everything(self) -> None:
        _auth(self.client, ["builder:manage"])
        self.assertEqual(self.client.get("/api/builder/workflows/").status_code, 200)
        self.assertNotEqual(
            self.client.get(f"/api/builder/workflows/{uuid.uuid4()}/").status_code, 403,
        )
