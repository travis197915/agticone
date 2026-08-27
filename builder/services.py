"""
Atomic bulk-save engine for the builder canvas.

`WorkflowGraphWriter` accepts the JSON the editor PUTs at
`/api/builder/workflows/:id/graph/` and reconciles the database with it in
one transaction:

  1. Diff each collection against what's stored.
  2. Create new rows (honouring client-supplied UUIDs + ``client_id``s).
  3. Update rows whose `id` survived.
  4. Delete rows that disappeared.
  5. Resolve connection endpoints by — in order — shape id or `client_id`
     from the same payload.
"""
from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from rest_framework import serializers as drf_serializers

from .bindings_sync import (
    extract_bindings_from_properties,
    workflow_rule_fingerprint,
    workflow_tool_fingerprint,
)
from .models import (
    Shape,
    ShapeConnection,
    ShapeDefinition,
    WorkArea,
    Workbench,
    Workflow,
)
from .workflow_versioning import snapshot_workflow_version


def _as_uuid(value: Any) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _pick(data: dict, keys: tuple[str, ...]) -> dict:
    return {k: data[k] for k in keys if k in data}


_WORKAREA_FIELDS = ("name", "description", "order", "color",
                    "position_x", "position_y", "width", "height", "metadata")
_WORKBENCH_FIELDS = ("name", "description", "node_key", "kind", "config", "order",
                     "position_x", "position_y", "width", "height", "style")
_SHAPE_FIELDS = ("label", "description", "position_x", "position_y",
                 "width", "height", "style", "properties", "order")
_CONNECTION_FIELDS = ("source_port", "target_port", "label",
                      "condition_label", "waypoints", "style")


class WorkflowGraphWriter:
    """One-shot atomic writer.  Instantiate, call :meth:`save`, throw away."""

    def __init__(self, workflow: Workflow) -> None:
        self.workflow = workflow
        # shape index keyed by id (str) and by `cid:<client_id>` — used by
        # the connection sync pass to resolve endpoints regardless of
        # whether the client referenced a real UUID or a temp id.
        self._shape_index: dict[str, uuid.UUID] = {}

    # ── public ───────────────────────────────────────────────────────────────

    @transaction.atomic
    def save(self, payload: dict) -> Workflow:
        # Workflow-level patches — restricted to update_fields so this can
        # never clobber OTHER fields (notably `version`, which
        # snapshot_workflow_version may bump concurrently, e.g. via a canvas
        # rule-change approval landing between when this Workflow instance
        # was fetched and this save call) with a stale in-memory value. An
        # unconditional wf.save() would silently write back whatever
        # `version` this instance happened to hold at fetch time.
        wf = self.workflow
        touched: list[str] = []
        if "name" in payload:
            wf.name = payload["name"]; touched.append("name")
        if "description" in payload:
            wf.description = payload["description"]; touched.append("description")
        if "is_active" in payload:
            wf.is_active = bool(payload["is_active"]); touched.append("is_active")
        if "metadata" in payload:
            wf.metadata = payload["metadata"] or {}; touched.append("metadata")
        if touched:
            wf.save(update_fields=[*touched, "updated_at"])

        if payload.get("work_areas") is not None:
            # Rule-content changes (add/edit/delete of an SOP-derived or
            # custom rule) must go through the pending-review flow —
            # builder.canvas_rule_changes.propose_canvas_rule_change /
            # approve_canvas_change_set — not this bulk graph save. Those
            # endpoints never mutate Shape.properties.sop_rules until
            # approved, so a well-behaved frontend save never carries a rule
            # diff here. This fingerprint check is a defensive backstop, not
            # the normal trigger: if one *is* detected, reject the save
            # instead of silently applying or silently versioning it, so a
            # rule edit can never bypass review. Structural-only changes
            # (position, connections, labels, tool_calls) are unaffected and
            # keep autosaving immediately.
            #
            # Exempt from this check: rules that live entirely on a shape
            # that did not exist before this save at all (a brand-new node
            # dragged onto the canvas and saved with its rules already
            # attached, or a brand-new SOP autobuild/attach). There is no
            # "previous state" to review a new shape's rules against, so
            # only a diff touching an ALREADY-EXISTING shape's rule set
            # (add/edit/delete) counts as something that must be proposed.
            #
            # Tool bindings (tool_calls[]) are a separate axis entirely and
            # are NEVER gated by review (auto-live, by design) — but a tool
            # add/edit/delete still changes what a claim will execute, so it
            # must still bump Workflow.version so reprocess detection
            # (execution_app.services.run_versions) sees it. Same treatment
            # for a brand-new shape's initial rules: exempt from review, but
            # not exempt from versioning — "exempt from review" and "exempt
            # from versioning" are different questions.
            before_fp = workflow_rule_fingerprint(wf)
            before_shape_ids = {t[0] for t in before_fp}
            before_tool_fp = workflow_tool_fingerprint(wf)
            # All shape ids, independent of rule/tool content — used only to
            # detect "a shape that existed is now gone" for versioning below.
            # A rule-less/decorative shape never appears in before_fp/after_fp
            # at all, so that comparison alone can't see it disappear.
            before_all_shape_ids = {
                str(sid) for sid in Shape.objects.filter(
                    workbench__work_area__workflow=wf
                ).values_list("id", flat=True)
            }
            self._sync_work_areas(payload["work_areas"])
            after_fp = workflow_rule_fingerprint(wf)
            # Whole-node deletion is a structural change, not a rule edit —
            # a deleted shape's rule tuples have nowhere to appear in
            # after_fp (the shape itself is gone), which must not be
            # confused with "this rule was edited/removed on a shape that
            # still exists." Scope the before-side of the comparison to
            # shapes that survived this save, so a deleted shape's rules are
            # simply exempt from the guard below rather than tripping it.
            after_all_shape_ids = {
                str(sid) for sid in Shape.objects.filter(
                    workbench__work_area__workflow=wf
                ).values_list("id", flat=True)
            }
            before_fp_on_surviving_shapes = {t for t in before_fp if t[0] in after_all_shape_ids}
            after_fp_on_existing_shapes = {t for t in after_fp if t[0] in before_shape_ids}
            after_tool_fp = workflow_tool_fingerprint(wf)
            if before_fp_on_surviving_shapes != after_fp_on_existing_shapes:
                raise drf_serializers.ValidationError({
                    "work_areas": (
                        "This save changes rule content on an existing node "
                        "(an SOP-derived or custom rule was added, edited, or "
                        "removed). Rule changes on an existing node must go "
                        "through POST /workflows/<id>/rule-changes/propose/ "
                        "and be approved, not saved directly via the graph "
                        "endpoint."
                    ),
                })

            new_shape_rules = after_fp - after_fp_on_existing_shapes
            tools_changed = before_tool_fp != after_tool_fp
            # Any shape gone, regardless of whether it ever carried a rule or
            # tool — a rule-less/decorative node's deletion must bump the
            # version exactly like a rule-bearing one does, so
            # run_version_info() sees every structural change, not just ones
            # with rule/tool content.
            shapes_deleted = bool(before_all_shape_ids - after_all_shape_ids)
            if new_shape_rules or tools_changed or shapes_deleted:
                reasons = []
                if new_shape_rules:
                    reasons.append("new_node_rules")
                if shapes_deleted:
                    reasons.append("node_deleted")
                if tools_changed:
                    reasons.append("tool_change")
                snapshot_workflow_version(wf, reason="+".join(reasons), force=True)

        if payload.get("connections") is not None:
            self._sync_connections(payload["connections"])

        return wf

    # ── work areas ───────────────────────────────────────────────────────────

    def _sync_work_areas(self, areas: list[dict]) -> None:
        existing = {wa.id: wa for wa in self.workflow.work_areas.all()}
        by_name = {wa.name: wa for wa in existing.values()}
        # A WorkArea that owns any snapshot-protected Workbench (see
        # _sync_workbenches below) must never be a stale-DELETE candidate —
        # deleting it would CASCADE into that Workbench and hit the same
        # WorkflowVersionWorkbench.workbench PROTECT FK, just one level up.
        # WorkArea has no is_current concept of its own, so this is the only
        # guard needed here. Kept separate from `existing` above so a
        # protected WorkArea that IS present in the payload is still matched
        # by id/name and updated normally — only the stale/delete computation
        # below excludes it.
        protected_ids = set(
            self.workflow.work_areas
            .filter(workbenches__version_snapshots__isnull=False)
            .values_list("id", flat=True)
        )
        keep: set[uuid.UUID] = set()

        for i, area_data in enumerate(areas):
            area_id = _as_uuid(area_data.get("id"))
            area = existing.get(area_id) if area_id else by_name.get(area_data.get("name", ""))

            patch = _pick(area_data, _WORKAREA_FIELDS)
            patch.setdefault("order", i)

            if area is not None:
                for k, v in patch.items():
                    setattr(area, k, v)
                area.save()
            else:
                create_kwargs = dict(patch)
                if area_id:
                    create_kwargs["id"] = area_id
                area = WorkArea.objects.create(workflow=self.workflow, **create_kwargs)

            keep.add(area.id)
            self._sync_workbenches(area, area_data.get("workbenches") or [])

        stale = [wa_id for wa_id in existing if wa_id not in keep and wa_id not in protected_ids]
        if stale:
            WorkArea.objects.filter(id__in=stale).delete()

    # ── workbenches ──────────────────────────────────────────────────────────

    def _sync_workbenches(self, area: WorkArea, benches: list[dict]) -> None:
        # Retired (is_current=False) rows are permanent, immutable history —
        # see builder.models.Workbench / sop_ingestion.services.workflow_rollout
        # ._version_workbench_for_rollout. The frontend canvas only ever knows
        # about the current workbench per node_key, so a retired one is never
        # part of the incoming payload; excluding it from `existing`/`by_key`
        # keeps it out of both matching AND the stale/delete computation below.
        existing = {wb.id: wb for wb in area.workbenches.filter(is_current=True)}
        by_key = {wb.node_key: wb for wb in existing.values() if wb.node_key}
        # A currently-live Workbench that's ALSO already snapshot-protected
        # (WorkflowVersionWorkbench.workbench, PROTECT) must never be treated
        # as stale either — e.g. an empty column the frontend structurally can
        # never re-send once a rule-edit snapshot has captured the workflow's
        # composition (buildGraphPayload derives workbenches purely from
        # shape groupings, so a zero-shape Workbench can never appear in any
        # save payload). Kept separate from `existing` so it's still matched
        # normally by id/node_key if a future payload DOES reference it —
        # only the stale/delete computation below excludes it.
        protected_ids = {wb.id for wb in existing.values() if wb.version_snapshots.exists()}
        keep: set[uuid.UUID] = set()

        for i, bench_data in enumerate(benches):
            wb_id = _as_uuid(bench_data.get("id"))
            wb = (existing.get(wb_id) if wb_id
                  else by_key.get(bench_data.get("node_key", "")))

            patch = _pick(bench_data, _WORKBENCH_FIELDS)
            patch.setdefault("order", i)

            if wb is not None:
                for k, v in patch.items():
                    setattr(wb, k, v)
                wb.save()
            else:
                create_kwargs = dict(patch)
                if wb_id:
                    create_kwargs["id"] = wb_id
                wb = Workbench.objects.create(work_area=area, **create_kwargs)

            keep.add(wb.id)
            self._sync_shapes(wb, bench_data.get("shapes") or [], bench_data.get("client_id"))

        stale = [wb_id for wb_id in existing if wb_id not in keep and wb_id not in protected_ids]
        if stale:
            Workbench.objects.filter(id__in=stale).delete()

    # ── shapes ───────────────────────────────────────────────────────────────

    def _sync_shapes(self, wb: Workbench, shapes: list[dict], _wb_cid: str | None) -> None:
        existing = {s.id: s for s in wb.shapes.all()}
        keep: set[uuid.UUID] = set()

        for i, shape_data in enumerate(shapes):
            shape_id = _as_uuid(shape_data.get("id"))
            shape = existing.get(shape_id) if shape_id else None

            slug = shape_data.get("definition_slug")
            if slug is None and shape is None:
                raise drf_serializers.ValidationError(
                    {"shapes": "New shapes must include 'definition_slug'."}
                )
            definition = None
            if slug is not None:
                try:
                    definition = ShapeDefinition.objects.get(slug=slug)
                except ShapeDefinition.DoesNotExist:
                    raise drf_serializers.ValidationError(
                        {"shapes": f"Unknown shape definition '{slug}'."}
                    )

            patch = _pick(shape_data, _SHAPE_FIELDS)
            patch.setdefault("order", i)

            if shape is not None:
                if definition is not None:
                    shape.definition = definition
                for k, v in patch.items():
                    setattr(shape, k, v)
                shape.save()
            else:
                create_kwargs = dict(patch)
                if shape_id:
                    create_kwargs["id"] = shape_id
                shape = Shape.objects.create(
                    workbench=wb, definition=definition, **create_kwargs,
                )

            keep.add(shape.id)
            self._shape_index[str(shape.id)] = shape.id
            cid = shape_data.get("client_id")
            if cid:
                self._shape_index[f"cid:{cid}"] = shape.id

            # Project the JSON-blob sop_rules / tool_calls on this shape
            # into the agent_tools binding tables. Safe when agent_tools
            # is missing (no-op).
            extract_bindings_from_properties(shape)

        stale = [s_id for s_id in existing if s_id not in keep]
        if stale:
            Shape.objects.filter(id__in=stale).delete()

    # ── connections ──────────────────────────────────────────────────────────

    def _sync_connections(self, conns: list[dict]) -> None:
        # Index every shape under this workflow so existing shapes referenced
        # by id resolve even when this payload doesn't redeclare them.
        all_shapes = Shape.objects.filter(
            workbench__work_area__workflow=self.workflow,
        ).values_list("id", flat=True)
        for sid in all_shapes:
            self._shape_index[str(sid)] = sid

        existing = {
            c.id: c
            for c in ShapeConnection.objects.filter(
                source_shape__workbench__work_area__workflow=self.workflow,
            )
        }
        natural: dict[tuple, ShapeConnection] = {
            (c.source_shape_id, c.target_shape_id, c.label, c.condition_label): c
            for c in existing.values()
        }
        keep: set[uuid.UUID] = set()

        for conn in conns:
            from_id = self._resolve_endpoint(conn, "source")
            to_id = self._resolve_endpoint(conn, "target")
            if from_id == to_id:
                raise drf_serializers.ValidationError(
                    {"connections": "source and target must differ."}
                )

            patch = _pick(conn, _CONNECTION_FIELDS)
            cid = _as_uuid(conn.get("id"))
            row = (existing.get(cid) if cid else
                   natural.get((from_id, to_id,
                                patch.get("label", ""),
                                patch.get("condition_label", ""))))

            if row is not None:
                row.source_shape_id = from_id
                row.target_shape_id = to_id
                for k, v in patch.items():
                    setattr(row, k, v)
                row.save()
            else:
                create_kwargs = dict(patch, source_shape_id=from_id, target_shape_id=to_id)
                if cid:
                    create_kwargs["id"] = cid
                row = ShapeConnection.objects.create(**create_kwargs)

            keep.add(row.id)

        stale = [c_id for c_id in existing if c_id not in keep]
        if stale:
            ShapeConnection.objects.filter(id__in=stale).delete()

    def _resolve_endpoint(self, conn: dict, side: str) -> uuid.UUID:
        direct = conn.get(f"{side}_shape_id") or conn.get(f"{side}_shape")
        if direct:
            hit = self._shape_index.get(str(direct))
            if not hit:
                raise drf_serializers.ValidationError(
                    {"connections": f"{side}_shape {direct} does not belong to this workflow."}
                )
            return hit

        cid = conn.get(f"{side}_client_id")
        if cid:
            hit = self._shape_index.get(f"cid:{cid}")
            if not hit:
                raise drf_serializers.ValidationError(
                    {"connections": f"No shape with client_id='{cid}' in this payload."}
                )
            return hit

        raise drf_serializers.ValidationError(
            {"connections": f"Connection {side} endpoint missing — supply id or client_id."}
        )
