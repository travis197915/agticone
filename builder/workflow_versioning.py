"""Historical workflow-composition snapshots.

Independent of ``builder.workbench_versioning`` (which decides *whether* one
Workbench's content changed) and of ``AuditSop.version_number``/``supersedes``
(the SOP-document identity axis). This module answers a different question:
"given the workflow's current set of live Workbenches, does that composition
need a new, permanent, queryable ``WorkflowVersion`` record — and if so, what
exactly was in it." See ``builder.models.WorkflowVersion``/
``WorkflowVersionWorkbench`` for the schema this writes.
"""
from __future__ import annotations

import logging

from django.db import transaction

from builder.models import Workbench, Workflow, WorkflowVersion, WorkflowVersionWorkbench

log = logging.getLogger(__name__)


def _current_slots(workflow) -> dict[str, "Workbench"]:
    """The workflow's live composition, one Workbench per node_key.

    Defensive against a known pre-existing data issue (see
    builder.sop_autobuild._reuse_existing_sops_for_job's docstring for the
    AuditSop-side version of the same problem): nothing at the DB level
    enforces "at most one is_current=True row per node_key", so more than
    one can exist. Ordered so the best candidate (highest version, then
    most recently touched) deterministically wins the slot rather than an
    unordered-queryset pick — WorkflowVersionWorkbench also has a
    (workflow_version, node_key) uniqueness constraint that would otherwise
    reject the snapshot outright when this happens.
    """
    slots: dict[str, Workbench] = {}
    dupes: dict[str, int] = {}
    qs = Workbench.objects.filter(
        work_area__workflow=workflow, is_current=True,
    ).order_by("-version", "-updated_at")
    for wb in qs:
        if wb.node_key in slots:
            dupes[wb.node_key] = dupes.get(wb.node_key, 1) + 1
            continue
        slots[wb.node_key] = wb
    if dupes:
        log.warning(
            "workflow=%s has multiple is_current=True Workbench rows sharing "
            "the same node_key (%s) — kept the highest-version/most-recent "
            "one per slot for this snapshot", workflow.id, dupes,
        )
    return slots


def _latest_version(workflow) -> WorkflowVersion | None:
    return (
        WorkflowVersion.objects
        .filter(workflow=workflow)
        .order_by("-version_number")
        .prefetch_related("slots")
        .first()
    )


def snapshot_workflow_version(workflow, reason: str) -> WorkflowVersion | None:
    """Create the next WorkflowVersion snapshot from the workflow's current
    (``is_current=True``) Workbenches, if the composition actually changed.

    Order of operations matters:
      1. Lock the workflow.
      2. Compute the live composition.
      3. Compare against the LATEST existing snapshot's composition — if
         identical, return that snapshot unchanged. ``Workflow.version`` is
         never written in this branch, so a defensive duplicate call can
         never accidentally increment the counter.
      4. If no snapshot exists yet for this workflow at all (the very first
         call — i.e. the initial build), create version_number=workflow.version
         AS-IS, with no increment (it's already 1, the model default).
      5. Otherwise (a real, later composition change), increment
         workflow.version and create the new snapshot at that value.

    Reason (4) is what makes the first build correctly land on "Workflow v1"
    instead of "v2" — the earlier draft of this feature incremented
    unconditionally and got this wrong.
    """
    reason = (reason or "")[:64]

    with transaction.atomic():
        workflow = Workflow.objects.select_for_update().get(pk=workflow.pk)

        current = _current_slots(workflow)
        current_set = {node_key: wb.id for node_key, wb in current.items()}

        latest = _latest_version(workflow)
        if latest is not None:
            latest_set = {s.node_key: s.workbench_id for s in latest.slots.all()}
            if latest_set == current_set:
                return latest  # true no-op — Workflow.version is NOT touched
            workflow.version += 1
            workflow.save(update_fields=["version", "updated_at"])
            version_number = workflow.version
        else:
            # First snapshot ever for this workflow — use the current value
            # as-is (the initial build never increments; see docstring).
            version_number = workflow.version

        snapshot = WorkflowVersion.objects.create(
            workflow=workflow, version_number=version_number, reason=reason,
        )

        if current:
            sop_ids = {
                (wb.config or {}).get("sop_id")
                for wb in current.values()
                if (wb.config or {}).get("sop_id")
            }
            sop_lookup: dict[int, dict] = {}
            if sop_ids:
                from sop_ingestion.models import AuditSop

                sop_lookup = {
                    row["id"]: row
                    for row in AuditSop.objects.filter(id__in=sop_ids).values(
                        "id", "title", "version_number",
                    )
                }

            WorkflowVersionWorkbench.objects.bulk_create([
                WorkflowVersionWorkbench(
                    workflow_version=snapshot,
                    workbench=wb,
                    node_key=wb.node_key,
                    order=wb.order,
                    workbench_version=wb.version,
                    audit_sop_id=(sop_lookup.get((wb.config or {}).get("sop_id")) or {}).get("id"),
                    sop_title=(sop_lookup.get((wb.config or {}).get("sop_id")) or {}).get(
                        "title", "") or (wb.config or {}).get("sop_title", ""),
                    sop_version_number=(sop_lookup.get((wb.config or {}).get("sop_id")) or {}).get(
                        "version_number"),
                )
                for wb in current.values()
            ])

        return snapshot
