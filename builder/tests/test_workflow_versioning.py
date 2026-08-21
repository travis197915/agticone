"""Tests for workflow-level versioning (builder.workflow_versioning) and the
Workbench write-once invariant it depends on (builder.models.Workbench).

NOTE: at the time this file was written, `manage.py test` fails during test
database creation with a pre-existing, unrelated migration/model drift
(``relation "execution_rule_evaluation" does not exist``) already present on
this branch before this feature — see git history / session notes. That
drift is out of scope for this change and is not touched here. These tests
are written to pass once that drift is resolved; in the meantime the same
behavior was verified via a rolled-back smoke script against the live dev
database (see PR description).
"""
from __future__ import annotations

import uuid

from django.test import TestCase

from builder.models import (
    Workbench, WorkbenchImmutableFieldError, WorkArea, Workflow,
    WorkflowVersion,
)
from builder.workflow_versioning import snapshot_workflow_version


def _workflow() -> Workflow:
    return Workflow.objects.create(
        name=f"WF-{uuid.uuid4().hex[:8]}",
        slug=f"wf-{uuid.uuid4().hex[:8]}",
        description="", is_active=True,
    )


def _bench(area: WorkArea, *, node_key: str, order: int, sop_id: int,
           content_hash: str, version: int = 1, is_current: bool = True) -> Workbench:
    return Workbench.objects.create(
        work_area=area, name=f"{order + 1}. SOP {node_key}", order=order,
        node_key=node_key, kind="SOP", version=version, is_current=is_current,
        config={
            "sop_id": sop_id, "sop_title": f"SOP {node_key.upper()}",
            "source_url": f"https://example.com/{node_key}", "content_hash": content_hash,
        },
    )


class SnapshotWorkflowVersionTests(TestCase):
    def setUp(self):
        self.wf = _workflow()
        self.area = WorkArea.objects.create(workflow=self.wf, name="Claim Audit", order=0)

    def test_initial_build_lands_on_v1_no_increment(self):
        _bench(self.area, node_key="a", order=0, sop_id=1, content_hash="a1")
        _bench(self.area, node_key="b", order=1, sop_id=2, content_hash="b1")

        snap = snapshot_workflow_version(self.wf, reason="initial_build")

        self.wf.refresh_from_db()
        self.assertEqual(self.wf.version, 1)
        self.assertEqual(snap.version_number, 1)
        self.assertEqual(WorkflowVersion.objects.filter(workflow=self.wf).count(), 1)
        self.assertEqual(snap.slots.count(), 2)

    def test_true_no_op_never_increments(self):
        _bench(self.area, node_key="a", order=0, sop_id=1, content_hash="a1")
        snapshot_workflow_version(self.wf, reason="initial_build")
        self.wf.refresh_from_db()

        snap2 = snapshot_workflow_version(self.wf, reason="defensive_duplicate_call")

        self.wf.refresh_from_db()
        self.assertEqual(self.wf.version, 1)
        self.assertEqual(WorkflowVersion.objects.filter(workflow=self.wf).count(), 1)
        self.assertEqual(snap2.version_number, 1)

    def test_composition_change_increments_and_reuses_unchanged_slot(self):
        wb_a1 = _bench(self.area, node_key="a", order=0, sop_id=1, content_hash="a1")
        wb_b1 = _bench(self.area, node_key="b", order=1, sop_id=2, content_hash="b1")
        v1 = snapshot_workflow_version(self.wf, reason="initial_build")

        # A changes: new Workbench row for slot "a", old retired.
        wb_a2 = Workbench.objects.create(
            work_area=self.area, name="1. SOP A", order=0, node_key="a",
            version=2, is_current=True,
            config={"sop_id": 1, "sop_title": "SOP A",
                    "source_url": "https://example.com/a", "content_hash": "a2"},
        )
        Workbench.objects.filter(pk=wb_a1.pk).update(is_current=False)

        v2 = snapshot_workflow_version(self.wf, reason="sop_content_changed")

        self.wf.refresh_from_db()
        self.assertEqual(self.wf.version, 2)
        self.assertEqual(v2.version_number, 2)

        slots_v2 = {s.node_key: s.workbench_id for s in v2.slots.all()}
        self.assertEqual(slots_v2["a"], wb_a2.id)
        self.assertEqual(slots_v2["b"], wb_b1.id)  # unchanged slot: SAME row reused

        # v1's own snapshot is untouched by v2's creation.
        slots_v1 = {s.node_key: s.workbench_id for s in v1.slots.all()}
        self.assertEqual(slots_v1["a"], wb_a1.id)
        self.assertEqual(slots_v1["b"], wb_b1.id)

    def test_new_sop_added_bumps_version(self):
        _bench(self.area, node_key="a", order=0, sop_id=1, content_hash="a1")
        snapshot_workflow_version(self.wf, reason="initial_build")
        self.wf.refresh_from_db()
        self.assertEqual(self.wf.version, 1)

        _bench(self.area, node_key="c", order=1, sop_id=3, content_hash="c1")
        snap = snapshot_workflow_version(self.wf, reason="sop_added")

        self.wf.refresh_from_db()
        self.assertEqual(self.wf.version, 2)
        self.assertEqual({s.node_key for s in snap.slots.all()}, {"a", "c"})

    def test_reorder_only_is_not_a_composition_change(self):
        wb_a = _bench(self.area, node_key="a", order=0, sop_id=1, content_hash="a1")
        wb_b = _bench(self.area, node_key="b", order=1, sop_id=2, content_hash="b1")
        snapshot_workflow_version(self.wf, reason="initial_build")
        self.wf.refresh_from_db()

        # Pure reorder — same Workbench rows, only `.order` changes. This
        # does not go through snapshot_workflow_version (sop_autobuild's
        # _reorder_sops never calls it), so simulate the "nothing calls
        # snapshot after a reorder" contract directly: composition (the
        # node_key -> workbench_id mapping) is unchanged, so a defensive
        # call would still no-op even if something did call it.
        Workbench.objects.filter(pk=wb_a.pk).update(order=1)
        Workbench.objects.filter(pk=wb_b.pk).update(order=0)

        snap = snapshot_workflow_version(self.wf, reason="defensive")
        self.wf.refresh_from_db()
        self.assertEqual(self.wf.version, 1)
        self.assertEqual(snap.version_number, 1)


class HeadlineAcceptanceTests(TestCase):
    """The actual business guarantee: an old claim's recorded snapshot never
    changes, even after the workflow moves on — and the Workbench rows it
    points at are never mutated."""

    def test_old_claim_stays_pinned_to_its_workflow_version(self):
        wf = _workflow()
        area = WorkArea.objects.create(workflow=wf, name="Claim Audit", order=0)
        wb_a1 = _bench(area, node_key="a", order=0, sop_id=1, content_hash="a1")
        wb_b1 = _bench(area, node_key="b", order=1, sop_id=2, content_hash="b1")
        v1 = snapshot_workflow_version(wf, reason="initial_build")
        wf.refresh_from_db()
        self.assertEqual(wf.version, 1)

        # A changes -> v2, B changes -> v3, A changes again -> v4.
        for node_key, new_hash, new_version in (("a", "a2", 2), ("b", "b2", 2), ("a", "a3", 3)):
            old = Workbench.objects.get(work_area=area, node_key=node_key, is_current=True)
            new = Workbench.objects.create(
                work_area=area, name=old.name, order=old.order, node_key=node_key,
                version=new_version, is_current=True,
                config={**old.config, "content_hash": new_hash},
            )
            Workbench.objects.filter(pk=old.pk).update(is_current=False)
            snapshot_workflow_version(wf, reason="sop_content_changed")

        wf.refresh_from_db()
        self.assertEqual(wf.version, 4)

        v1.refresh_from_db()
        slots = {s.node_key: (s.workbench_id, s.workbench_version) for s in v1.slots.all()}
        self.assertEqual(slots, {"a": (wb_a1.id, 1), "b": (wb_b1.id, 1)})

        # The Workbench rows v1 points at were never mutated.
        wb_a1.refresh_from_db()
        wb_b1.refresh_from_db()
        self.assertEqual(wb_a1.version, 1)
        self.assertEqual(wb_a1.config["content_hash"], "a1")
        self.assertEqual(wb_b1.version, 1)
        self.assertEqual(wb_b1.config["content_hash"], "b1")


class WorkbenchWriteOnceGuardTests(TestCase):
    def setUp(self):
        wf = _workflow()
        area = WorkArea.objects.create(workflow=wf, name="Claim Audit", order=0)
        self.wb = _bench(area, node_key="a", order=0, sop_id=1, content_hash="a1")

    def test_version_is_write_once_via_save(self):
        wb = Workbench.objects.get(pk=self.wb.pk)
        wb.version = 2
        with self.assertRaises(WorkbenchImmutableFieldError):
            wb.save()

    def test_node_key_is_write_once_via_save(self):
        wb = Workbench.objects.get(pk=self.wb.pk)
        wb.node_key = "b"
        with self.assertRaises(WorkbenchImmutableFieldError):
            wb.save()

    def test_config_identity_keys_are_write_once_via_save(self):
        wb = Workbench.objects.get(pk=self.wb.pk)
        wb.config = {**wb.config, "content_hash": "different"}
        with self.assertRaises(WorkbenchImmutableFieldError):
            wb.save()

    def test_config_non_identity_keys_remain_mutable(self):
        wb = Workbench.objects.get(pk=self.wb.pk)
        wb.config = {**wb.config, "extra_context": "auditor note"}
        wb.save()  # must not raise
        wb.refresh_from_db()
        self.assertEqual(wb.config["extra_context"], "auditor note")
        self.assertEqual(wb.config["content_hash"], "a1")  # identity untouched

    def test_order_and_is_current_remain_mutable(self):
        wb = Workbench.objects.get(pk=self.wb.pk)
        wb.order = 5
        wb.is_current = False
        wb.save()  # must not raise

    def test_bulk_update_blocks_protected_fields(self):
        with self.assertRaises(WorkbenchImmutableFieldError):
            Workbench.objects.filter(pk=self.wb.pk).update(version=2)
        with self.assertRaises(WorkbenchImmutableFieldError):
            Workbench.objects.filter(pk=self.wb.pk).update(node_key="z")
        with self.assertRaises(WorkbenchImmutableFieldError):
            Workbench.objects.filter(pk=self.wb.pk).update(config={"sop_id": 999})

    def test_bulk_update_allows_unprotected_fields(self):
        Workbench.objects.filter(pk=self.wb.pk).update(order=9)  # must not raise
        self.wb.refresh_from_db()
        self.assertEqual(self.wb.order, 9)

    def test_creation_is_unaffected_by_the_guard(self):
        # A fresh Workbench.objects.create(...) is a creation, not a
        # mutation — the guard must never fire for it, regardless of what
        # version/node_key/config values are supplied.
        wb = Workbench.objects.create(
            work_area=self.wb.work_area, name="Fresh", order=1,
            node_key="fresh", version=1,
            config={"sop_id": 99, "content_hash": "x"},
        )
        self.assertEqual(wb.version, 1)


def _shape_for(workbench: Workbench):
    """Minimal Shape fixture — resolves whatever ShapeDefinition catalog row
    already exists rather than depending on the seeded 'rectangle' slug."""
    from builder.models import Shape, ShapeCategory, ShapeDefinition

    cat, _ = ShapeCategory.objects.get_or_create(
        slug="test-cat", defaults={"label": "Test", "order": 0, "is_active": True},
    )
    sd, _ = ShapeDefinition.objects.get_or_create(
        slug="test-rect", defaults={
            "category": cat, "label": "Test", "kind": "rectangle",
            "svg_path": "M0 0h100v100H0z",
        },
    )
    return Shape.objects.create(workbench=workbench, definition=sd, label="S")


class RolloutWorkbenchVersioningTests(TestCase):
    """sop_ingestion.services.rule_changes._version_workbench_for_rollout —
    exercised directly with a mocked plan_rollout report, matching the
    pattern used to smoke-test the original in-place bump before this
    revision replaced it with the new-row design."""

    def test_no_bump_when_only_preserved(self):
        from unittest.mock import patch

        from sop_ingestion.services.rule_changes import _version_workbench_for_rollout
        from sop_ingestion.services.workflow_rollout import RolloutPlan, RolloutReport

        wf = _workflow()
        area = WorkArea.objects.create(workflow=wf, name="Claim Audit", order=0)
        wb = _bench(area, node_key="a", order=0, sop_id=1, content_hash="a1")

        fake_plan = RolloutPlan(report=RolloutReport(preserved=3))
        with patch(
            "sop_ingestion.services.workflow_rollout.preview_rollout",
            return_value=fake_plan,
        ):
            _version_workbench_for_rollout(wf, from_sop=object(), to_sop=object())

        self.assertEqual(Workbench.objects.filter(work_area=area).count(), 1)
        wb.refresh_from_db()
        self.assertTrue(wb.is_current)
        self.assertEqual(wb.version, 1)

    def test_real_change_creates_new_row_and_preserves_old(self):
        from unittest.mock import MagicMock, patch

        from sop_ingestion.services.rule_changes import _version_workbench_for_rollout
        from sop_ingestion.services.workflow_rollout import RolloutPlan, RolloutReport

        wf = _workflow()
        area = WorkArea.objects.create(workflow=wf, name="Claim Audit", order=0)
        old = _bench(area, node_key="a", order=0, sop_id=1, content_hash="a1")
        shape = _shape_for(old)

        to_sop = MagicMock(id=2, title="SOP A", url="https://example.com/a",
                            canonical_url="", content_hash="a2")
        fake_plan = RolloutPlan(report=RolloutReport(repointed=1))
        with patch(
            "sop_ingestion.services.workflow_rollout.preview_rollout",
            return_value=fake_plan,
        ):
            _version_workbench_for_rollout(wf, from_sop=MagicMock(), to_sop=to_sop)

        self.assertEqual(Workbench.objects.filter(work_area=area).count(), 2)
        old.refresh_from_db()
        self.assertFalse(old.is_current)
        self.assertEqual(old.version, 1)
        self.assertEqual(old.config["content_hash"], "a1")  # untouched, forever

        new = Workbench.objects.exclude(pk=old.pk).get(work_area=area)
        self.assertTrue(new.is_current)
        self.assertEqual(new.version, 2)
        self.assertEqual(new.node_key, "a")
        self.assertEqual(new.config["content_hash"], "a2")

        shape.refresh_from_db()
        self.assertEqual(shape.workbench_id, new.id)  # re-parented, not cloned

    def test_bump_when_only_orphaned(self):
        """A rollout that only orphans hand-edited rules (no repoint/refresh/
        drop) still counts as a real content change — the shape's properties
        gained a new custom rule, so the canvas moved even though no binding
        was repointed onto the new SOP."""
        from unittest.mock import MagicMock, patch

        from sop_ingestion.services.rule_changes import _version_workbench_for_rollout
        from sop_ingestion.services.workflow_rollout import RolloutPlan, RolloutReport

        wf = _workflow()
        area = WorkArea.objects.create(workflow=wf, name="Claim Audit", order=0)
        _bench(area, node_key="a", order=0, sop_id=1, content_hash="a1")

        to_sop = MagicMock(id=2, title="SOP A", url="https://example.com/a",
                            canonical_url="", content_hash="a2")
        fake_plan = RolloutPlan(report=RolloutReport(orphaned=1))
        with patch(
            "sop_ingestion.services.workflow_rollout.preview_rollout",
            return_value=fake_plan,
        ):
            _version_workbench_for_rollout(wf, from_sop=MagicMock(), to_sop=to_sop)

        self.assertEqual(Workbench.objects.filter(work_area=area).count(), 2)


def _rule_sop() -> "AuditSop":
    from sop_ingestion.models import ActivationStatus, AuditSop, IngestionJob

    job = IngestionJob.objects.create(seed_url="https://example.com/rule-sop.html")
    return AuditSop.objects.create(
        job=job, url="https://example.com/rule-sop.html",
        content_hash="rule-sop-hash", title="Rule Test SOP",
        is_current=True, activation_status=ActivationStatus.ACTIVE, version_number=1,
    )


class GraphSaveRuleChangeGuardTests(TestCase):
    """builder.services.WorkflowGraphWriter.save() must REJECT a rule-content
    change (add/edit/delete of an SOP-derived or custom rule) to a shape that
    already existed before the save — that must go through
    builder.canvas_rule_changes' propose/approve flow instead (see
    CanvasRuleChangeReviewTests below). A brand-new shape's initial rules
    (nothing existed before this save to review against) and pure
    structural/ordering-only changes on an existing shape are exempt and
    still autosave immediately, exactly as before this feature.
    """

    def setUp(self):
        from builder.services import WorkflowGraphWriter

        self.sop = _rule_sop()
        self.wf = _workflow()
        self.area = WorkArea.objects.create(workflow=self.wf, name="Area", order=0)
        self.wb = _bench(
            self.area, node_key="n1", order=0, sop_id=self.sop.id, content_hash="h1",
        )
        self.shape = _shape_for(self.wb)
        self.writer_cls = WorkflowGraphWriter

    def _payload(self, rules: list[dict], *, shape_id=None, label: str = "S") -> dict:
        return {
            "work_areas": [{
                "id": str(self.area.id), "name": "Area", "order": 0,
                "workbenches": [{
                    "id": str(self.wb.id), "name": "Node 1", "node_key": "n1",
                    "kind": "SOP", "order": 0,
                    "shapes": [{
                        "id": str(shape_id or self.shape.id), "definition_slug": "test-rect",
                        "label": label, "order": 0,
                        "properties": {"sop_rules": rules, "tool_calls": []},
                    }],
                }],
            }],
        }

    def _set_existing_rules(self, rules: list[dict]) -> None:
        """Establish 'pre-existing, already-saved' rule state the same way
        the real system always does — via extract_bindings_from_properties,
        so the NodeRuleBinding rows exist before the save under test. Without
        this, a rule's FIRST-ever binding creation (unbound raw JSON →
        bound NodeRuleBinding) would itself look like a content change to
        workflow_rule_fingerprint (sop_version_number appears for the first
        time), which never happens in production — every real write path
        (sop_autobuild, WorkflowGraphWriter, canvas_rule_changes) binds in
        the same operation that sets properties.
        """
        from builder.bindings_sync import extract_bindings_from_properties

        self.shape.properties = {"sop_rules": rules, "tool_calls": []}
        self.shape.save(update_fields=["properties"])
        extract_bindings_from_properties(self.shape)

    def test_new_shape_with_rules_is_exempt(self):
        from rest_framework.exceptions import ValidationError

        new_shape_id = uuid.uuid4()
        rules = [{"key": f"step:{self.sop.id}:1:0", "sop_id": self.sop.id,
                   "condition": "age < 18", "action": "DENY"}]
        try:
            self.writer_cls(self.wf).save(self._payload(rules, shape_id=new_shape_id))
        except ValidationError:
            self.fail("a brand-new shape's initial rules must not be gated")
        self.assertEqual(WorkflowVersion.objects.filter(workflow=self.wf).count(), 0)

    def test_adding_a_rule_to_an_existing_shape_is_rejected(self):
        from rest_framework.exceptions import ValidationError

        rules = [{"key": f"step:{self.sop.id}:1:0", "sop_id": self.sop.id,
                   "condition": "age < 18", "action": "DENY"}]
        with self.assertRaises(ValidationError):
            self.writer_cls(self.wf).save(self._payload(rules))

    def test_editing_an_existing_rule_is_rejected(self):
        from rest_framework.exceptions import ValidationError

        rule = {"key": f"step:{self.sop.id}:1:0", "sop_id": self.sop.id,
                 "condition": "age < 18", "action": "DENY"}
        self._set_existing_rules([rule])

        edited = {**rule, "condition": "age < 21"}
        with self.assertRaises(ValidationError):
            self.writer_cls(self.wf).save(self._payload([edited]))

    def test_deleting_an_existing_rule_is_rejected(self):
        from rest_framework.exceptions import ValidationError

        rule = {"key": f"step:{self.sop.id}:1:0", "sop_id": self.sop.id,
                 "condition": "age < 18", "action": "DENY"}
        self._set_existing_rules([rule])

        with self.assertRaises(ValidationError):
            self.writer_cls(self.wf).save(self._payload([]))

    def test_ordering_only_shift_on_existing_shape_is_allowed(self):
        from rest_framework.exceptions import ValidationError

        rule_a = {"key": f"step:{self.sop.id}:1:0", "sop_id": self.sop.id,
                   "condition": "age < 18", "action": "DENY"}
        rule_b = {"key": "custom:bbbb", "sop_id": 0, "is_custom": True,
                   "condition": "amount > 10000", "action": "REFER"}
        self._set_existing_rules([rule_a, rule_b])

        try:
            self.writer_cls(self.wf).save(self._payload([rule_b, rule_a]))
        except ValidationError:
            self.fail("a pure reorder of existing rules must not be gated")

    def test_noop_save_on_existing_shape_is_allowed(self):
        from rest_framework.exceptions import ValidationError

        rule = {"key": f"step:{self.sop.id}:1:0", "sop_id": self.sop.id,
                 "condition": "age < 18", "action": "DENY"}
        self._set_existing_rules([rule])

        try:
            self.writer_cls(self.wf).save(self._payload([rule]))
        except ValidationError:
            self.fail("an identical rule set must not be gated")

    def test_structural_only_change_on_existing_shape_is_allowed(self):
        rule = {"key": f"step:{self.sop.id}:1:0", "sop_id": self.sop.id,
                 "condition": "age < 18", "action": "DENY"}
        self._set_existing_rules([rule])

        self.writer_cls(self.wf).save(self._payload([rule], label="Renamed"))
        self.shape.refresh_from_db()
        self.assertEqual(self.shape.label, "Renamed")


class CanvasRuleChangeReviewTests(TestCase):
    """builder.canvas_rule_changes, dispatched from
    sop_ingestion.services.rule_changes.approve_change_set /
    change_set_payload for ChangeSetSource.CANVAS batches:

    * propose never touches Shape.properties or NodeRuleBinding.
    * approve applies every proposal in the batch, THEN creates exactly one
      WorkflowVersion/WorkflowVersionRule snapshot reflecting the complete
      post-change rule state.
    * reject leaves live rule state AND version history untouched.
    """

    def setUp(self):
        self.sop = _rule_sop()
        self.wf = _workflow()
        self.area = WorkArea.objects.create(workflow=self.wf, name="Area", order=0)
        self.wb = _bench(
            self.area, node_key="n1", order=0, sop_id=self.sop.id, content_hash="h1",
        )
        self.shape = _shape_for(self.wb)
        self.rule_key = f"step:{self.sop.id}:1:0"
        self.shape.properties = {
            "sop_rules": [{"key": self.rule_key, "sop_id": self.sop.id,
                            "condition": "age < 18", "action": "DENY"}],
            "tool_calls": [],
        }
        self.shape.save(update_fields=["properties"])
        from builder.bindings_sync import extract_bindings_from_properties
        extract_bindings_from_properties(self.shape)

    def _propose(self, *, kind, fields, rule_key=None, is_custom=False, author="a@x.com"):
        from builder.canvas_rule_changes import propose_canvas_rule_change

        return propose_canvas_rule_change(
            workflow=self.wf, shape=self.shape, rule_key=rule_key or self.rule_key,
            kind=kind, fields=fields, is_custom=is_custom, author=author,
        )

    def test_propose_edit_does_not_touch_shape_or_bindings(self):
        from sop_ingestion.models import RuleChangeKind

        before_props = dict(self.shape.properties)
        self._propose(kind=RuleChangeKind.MODIFIED, fields={"condition": "age < 21"})

        self.shape.refresh_from_db()
        self.assertEqual(self.shape.properties, before_props)
        self.assertEqual(WorkflowVersion.objects.filter(workflow=self.wf).count(), 0)

    def test_approve_applies_change_and_creates_exactly_one_version(self):
        from sop_ingestion.models import ChangeSetStatus, RuleChangeKind
        from sop_ingestion.services.rule_changes import approve_change_set

        proposal = self._propose(kind=RuleChangeKind.MODIFIED, fields={"condition": "age < 21"})
        change_set = proposal.changeset

        result = approve_change_set(
            change_set, proposal_ids=[proposal.id], reviewer="reviewer@x.com",
        )

        self.shape.refresh_from_db()
        rules = self.shape.properties["sop_rules"]
        self.assertEqual(rules[0]["condition"], "age < 21")

        self.assertEqual(WorkflowVersion.objects.filter(workflow=self.wf).count(), 1)
        v1 = WorkflowVersion.objects.get(workflow=self.wf)
        row = v1.rules.get(rule_key=self.rule_key)
        self.assertEqual(row.condition, "age < 21")

        change_set.refresh_from_db()
        self.assertEqual(change_set.status, ChangeSetStatus.APPROVED)
        self.assertEqual(result["resulting_version"], change_set.resulting_version)

    def test_reject_leaves_shape_and_versions_untouched(self):
        from sop_ingestion.models import ChangeSetStatus, RuleChangeKind
        from sop_ingestion.services.rule_changes import reject_change_set

        before_props = dict(self.shape.properties)
        proposal = self._propose(kind=RuleChangeKind.MODIFIED, fields={"condition": "age < 21"})
        change_set = proposal.changeset

        reject_change_set(change_set, reviewer="reviewer@x.com", note="not needed")

        self.shape.refresh_from_db()
        self.assertEqual(self.shape.properties, before_props)
        self.assertEqual(WorkflowVersion.objects.filter(workflow=self.wf).count(), 0)
        change_set.refresh_from_db()
        self.assertEqual(change_set.status, ChangeSetStatus.REJECTED)

    def test_custom_rule_add_then_delete_each_require_separate_approval(self):
        from sop_ingestion.models import RuleChangeKind
        from sop_ingestion.services.rule_changes import approve_change_set

        add_proposal = self._propose(
            kind=RuleChangeKind.ADDED, rule_key="custom:zzzz", is_custom=True,
            fields={"condition": "amount > 10000", "action": "REFER"},
        )
        approve_change_set(
            add_proposal.changeset, proposal_ids=[add_proposal.id], reviewer="r@x.com",
        )
        self.shape.refresh_from_db()
        keys = {r["key"] for r in self.shape.properties["sop_rules"]}
        self.assertIn("custom:zzzz", keys)
        self.assertEqual(WorkflowVersion.objects.filter(workflow=self.wf).count(), 1)

        del_proposal = self._propose(kind=RuleChangeKind.REMOVED, rule_key="custom:zzzz", fields={})
        approve_change_set(
            del_proposal.changeset, proposal_ids=[del_proposal.id], reviewer="r@x.com",
        )
        self.shape.refresh_from_db()
        keys = {r["key"] for r in self.shape.properties["sop_rules"]}
        self.assertNotIn("custom:zzzz", keys)
        self.assertEqual(WorkflowVersion.objects.filter(workflow=self.wf).count(), 2)

    def test_noop_edit_raises_no_effective_change(self):
        from sop_ingestion.models import RuleChangeKind
        from sop_ingestion.services.rule_changes import NoEffectiveChange

        with self.assertRaises(NoEffectiveChange):
            self._propose(kind=RuleChangeKind.MODIFIED, fields={"condition": "age < 18"})

    def test_stale_batch_rejected_at_approve(self):
        from sop_ingestion.models import ChangeSetSource, RuleChangeKind
        from sop_ingestion.services.rule_changes import ChangeSetStale, approve_change_set

        proposal = self._propose(kind=RuleChangeKind.MODIFIED, fields={"condition": "age < 21"})
        change_set = proposal.changeset
        self.assertEqual(change_set.source, ChangeSetSource.CANVAS)

        # Simulate another author's canvas edit landing (bumps Workflow.version)
        # in between propose and approve. force=True bumps unconditionally,
        # regardless of composition, without needing to fake an (immutable)
        # Workbench.version.
        from builder.workflow_versioning import snapshot_workflow_version
        snapshot_workflow_version(self.wf, reason="rule_edit", force=True)

        with self.assertRaises(ChangeSetStale):
            approve_change_set(change_set, proposal_ids=[proposal.id], reviewer="r@x.com")

    def test_historical_rule_rows_survive_shape_deletion(self):
        from builder.models import Shape, WorkflowVersionRule
        from sop_ingestion.models import RuleChangeKind
        from sop_ingestion.services.rule_changes import approve_change_set

        proposal = self._propose(kind=RuleChangeKind.MODIFIED, fields={"condition": "age < 21"})
        approve_change_set(proposal.changeset, proposal_ids=[proposal.id], reviewer="r@x.com")
        v1 = WorkflowVersion.objects.get(workflow=self.wf)
        row_id = v1.rules.get(rule_key=self.rule_key).id

        Shape.objects.filter(pk=self.shape.pk).delete()

        self.assertTrue(WorkflowVersionRule.objects.filter(pk=row_id).exists())
