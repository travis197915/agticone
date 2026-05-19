"""
End-to-end smoke test for the Django builder API.

We mint a JWT here with the same secret the Node corebackend uses, then drive
every public endpoint via Django's test client — exactly what the SPA does.

Run with:
    pytest -q builder/tests_smoke.py            # if pytest-django is set up
    # or
    python manage.py test builder.tests_smoke --settings=sop_backend.settings
"""
from __future__ import annotations

import json
import os
import uuid

import jwt
from django.test import Client, TransactionTestCase

from builder.catalog_seed import seed_all
from builder.models import (
    DashboardWidget,
    NavItem,
    ShapeCategory,
    ShapeConnection,
    ShapeDefinition,
    Workbench,
    Workflow,
)


def _mint_token(*, role: str = "ADMIN", email: str = "smoke@toystack.dev") -> str:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET must be set in env for the smoke test.")
    return jwt.encode(
        {"sub": f"user-{uuid.uuid4().hex[:8]}", "email": email, "role": role},
        secret, algorithm="HS256",
    )


class BuilderEndToEndTest(TransactionTestCase):
    def setUp(self) -> None:
        seed_all()
        self.client = Client()
        self.token = _mint_token()
        self.auth = f"Bearer {self.token}"

    # ── Tiny helpers ─────────────────────────────────────────────────────────

    def _get(self, url: str, expect: int = 200):
        r = self.client.get(url, HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(r.status_code, expect, r.content)
        return r.json() if r["Content-Type"].startswith("application/json") else r.content

    def _post(self, url: str, body, expect: int = 201):
        r = self.client.post(url, data=json.dumps(body),
                             content_type="application/json",
                             HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(r.status_code, expect, r.content)
        return r.json() if r.content else None

    def _put(self, url: str, body, expect: int = 200):
        r = self.client.put(url, data=json.dumps(body),
                            content_type="application/json",
                            HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(r.status_code, expect, r.content)
        return r.json() if r.content else None

    # ── 1. Anonymous access denied ───────────────────────────────────────────

    def test_anonymous_is_blocked(self):
        r = self.client.get("/api/builder/workflows/")
        self.assertIn(r.status_code, {401, 403})

    # ── 2. Catalog + chrome are populated ────────────────────────────────────

    def test_catalog_endpoints(self):
        cats = self._get("/api/builder/catalog/categories/")
        self.assertGreaterEqual(len(cats), 1)
        self.assertIn("general", {c["slug"] for c in cats})

        shapes = self._get("/api/builder/catalog/shapes/")
        self.assertGreaterEqual(len(shapes), 25)
        self.assertTrue(any(s["slug"] == "diamond" for s in shapes))
        diamond = next(s for s in shapes if s["slug"] == "diamond")
        self.assertEqual(diamond["kind"], "diamond")
        self.assertGreater(len(diamond["ports"]), 0)
        self.assertGreater(len(diamond["property_schema"]), 0)

        nav = self._get("/api/builder/ui/navigation/")
        self.assertGreaterEqual(len(nav), 5)
        self.assertIn("workflows", {n["slug"] for n in nav})

        widgets = self._get("/api/builder/ui/dashboard/")
        self.assertGreaterEqual(len(widgets), 3)

    def test_member_cannot_see_admin_nav(self):
        member_token = _mint_token(role="MEMBER")
        r = self.client.get(
            "/api/builder/ui/navigation/",
            HTTP_AUTHORIZATION=f"Bearer {member_token}",
        )
        self.assertEqual(r.status_code, 200)
        slugs = {n["slug"] for n in r.json()}
        self.assertNotIn("users", slugs, "Users page is ADMIN-only")

    # ── 3. Workflow CRUD + atomic graph bulk save ─────────────────────────────

    def test_full_workflow_lifecycle(self):
        # Create
        wf = self._post("/api/builder/workflows/",
                        {"name": "OBH Coverage Audit",
                         "description": "smoke test workflow"})
        wf_id = wf["id"]
        self.assertEqual(wf["slug"], "obh-coverage-audit")
        self.assertEqual(wf["owner_email"], "smoke@toystack.dev")

        # Bulk save the canvas — drag-drop style payload
        payload = {
            "work_areas": [{
                "name": "Intake",
                "position_x": 40, "position_y": 40,
                "workbenches": [{
                    "client_id": "wb-eligibility",
                    "name": "Eligibility",
                    "node_key": "eligibility",
                    "kind": "swimlane",
                    "shapes": [
                        {"client_id": "s-trigger",
                         "definition_slug": "ellipse",
                         "label": "Start",
                         "position_x": 40, "position_y": 40},
                        {"client_id": "s-decision",
                         "definition_slug": "diamond",
                         "label": "Member found?",
                         "position_x": 200, "position_y": 40,
                         "properties": {"yesLabel": "Yes", "noLabel": "No"}},
                    ],
                }],
            }, {
                "name": "Outcome",
                "position_x": 40, "position_y": 360,
                "workbenches": [{
                    "client_id": "wb-outcome",
                    "name": "Decision",
                    "node_key": "decision",
                    "shapes": [
                        {"client_id": "s-deny",
                         "definition_slug": "document",
                         "label": "Denial letter",
                         "position_x": 40, "position_y": 40},
                        {"client_id": "s-approve",
                         "definition_slug": "rect-rounded",
                         "label": "Approve",
                         "position_x": 220, "position_y": 40},
                    ],
                }],
            }],
            "connections": [
                {"source_client_id": "s-trigger",  "target_client_id": "s-decision"},
                {"source_client_id": "s-decision", "target_client_id": "s-approve",
                 "condition_label": "yes", "label": "covered"},
                {"source_client_id": "s-decision", "target_client_id": "s-deny",
                 "condition_label": "no",  "label": "not covered"},
            ],
        }

        graph = self._put(f"/api/builder/workflows/{wf_id}/graph/", payload)
        self.assertEqual(len(graph["work_areas"]), 2)
        self.assertEqual(len(graph["connections"]), 3)
        # Each shape should round-trip its definition slug
        slugs = [s["definition_slug"]
                 for wa in graph["work_areas"]
                 for wb in wa["workbenches"]
                 for s in wb["shapes"]]
        self.assertEqual(sorted(slugs), ["diamond", "document", "ellipse", "rect-rounded"])

        # Verify a cross-workbench edge exists
        wb_of_shape = {}
        for wa in graph["work_areas"]:
            for wb in wa["workbenches"]:
                for s in wb["shapes"]:
                    wb_of_shape[s["id"]] = wb["id"]
        crossed = [
            c for c in graph["connections"]
            if wb_of_shape.get(c["source_shape"]) != wb_of_shape.get(c["target_shape"])
        ]
        self.assertEqual(len(crossed), 2,
                         "Decision → Approve/Deny edges live in another workbench")

        # Idempotent re-save — strip server ids, ensure no dupes
        re_payload = json.loads(json.dumps(payload))  # deep copy
        graph2 = self._put(f"/api/builder/workflows/{wf_id}/graph/", re_payload)
        self.assertEqual(len(graph2["connections"]), 3)

        # Validation: self-loop rejected
        shape_id = next(
            s["id"] for wa in graph2["work_areas"]
            for wb in wa["workbenches"]
            for s in wb["shapes"] if s["label"] == "Start"
        )
        r = self.client.put(
            f"/api/builder/workflows/{wf_id}/graph/",
            data=json.dumps({"connections": [
                {"source_shape": shape_id, "target_shape": shape_id},
            ]}),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("source and target", r.content.decode())

        # Validation: unknown shape rejected
        r = self.client.put(
            f"/api/builder/workflows/{wf_id}/graph/",
            data=json.dumps({"connections": [
                {"source_shape": shape_id,
                 "target_shape": str(uuid.uuid4())},
            ]}),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(r.status_code, 400)

        # Duplicate
        dup = self._post(
            f"/api/builder/workflows/{wf_id}/duplicate/",
            {"name": "OBH Coverage Audit v2"},
        )
        self.assertNotEqual(dup["id"], wf_id)
        self.assertFalse(dup["is_active"])
        dup_graph = self._get(f"/api/builder/workflows/{dup['id']}/graph/")
        self.assertEqual(len(dup_graph["connections"]), 3)

        # Activate / deactivate
        self._post(f"/api/builder/workflows/{wf_id}/deactivate/", {}, expect=200)
        self.assertFalse(self._get(f"/api/builder/workflows/{wf_id}/")["is_active"])
        self._post(f"/api/builder/workflows/{wf_id}/activate/", {}, expect=200)
        self.assertTrue(self._get(f"/api/builder/workflows/{wf_id}/")["is_active"])

        # Verify DB counts
        self.assertEqual(Workflow.objects.count(), 2)
        self.assertEqual(Workbench.objects.filter(work_area__workflow_id=wf_id).count(), 2)
        self.assertEqual(ShapeConnection.objects.filter(
            source_shape__workbench__work_area__workflow_id=wf_id).count(), 3)

    # ── 4. Catalog is locked while in use ────────────────────────────────────

    def test_cannot_delete_shape_definition_in_use(self):
        # Quick: drop a shape, then try to delete its definition.
        wf = self._post("/api/builder/workflows/", {"name": "lock test"})
        self._put(f"/api/builder/workflows/{wf['id']}/graph/", {
            "work_areas": [{
                "name": "x", "workbenches": [{
                    "name": "y", "shapes": [
                        {"definition_slug": "rect", "label": "z"},
                    ],
                }],
            }],
        })
        # Even via the ORM, on_delete=PROTECT should refuse
        rect = ShapeDefinition.objects.get(slug="rect")
        with self.assertRaises(Exception):
            rect.delete()
