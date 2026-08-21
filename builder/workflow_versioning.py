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

from builder.models import (
    Workbench,
    Workflow,
    WorkflowVersion,
    WorkflowVersionRule,
    WorkflowVersionTool,
    WorkflowVersionWorkbench,
)

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


def snapshot_workflow_version(
    workflow, reason: str, *, force: bool = False,
) -> WorkflowVersion | None:
    """Create the next WorkflowVersion snapshot from the workflow's current
    (``is_current=True``) Workbenches and their shapes' rules, if anything
    actually changed.

    Order of operations matters:
      1. Lock the workflow.
      2. Compute the live composition.
      3. Compare against the LATEST existing snapshot's composition — if
         identical AND ``force`` is not set, return that snapshot unchanged.
         ``Workflow.version`` is never written in this branch, so a defensive
         duplicate call can never accidentally increment the counter.
      4. If no snapshot exists yet for this workflow at all (the very first
         call — i.e. the initial build), create version_number=workflow.version
         AS-IS, with no increment (it's already 1, the model default).
      5. Otherwise (a real, later composition change, or ``force=True``),
         increment workflow.version and create the new snapshot at that value.

    Reason (4) is what makes the first build correctly land on "Workflow v1"
    instead of "v2" — the earlier draft of this feature incremented
    unconditionally and got this wrong.

    ``force`` exists for callers that already know a *rule-content* change
    happened (builder.services.WorkflowGraphWriter, via
    builder.bindings_sync.workflow_rule_fingerprint) even though Workbench
    *composition* — all this function's own dedup check looks at — is
    unchanged. It deliberately does NOT widen the composition check itself:
    an ordinary node add/move/delete via WorkflowGraphWriter must not start
    silently creating versions just because this function is now reachable
    from that path — only an explicit, already-detected rule-content change
    (``force=True``) bypasses the no-op return.

    Whenever a new snapshot IS created (via any caller), the workflow's
    complete rule set — every NodeRuleBinding override and every custom
    ``sop_rules[]`` entry, across every current shape — is materialized into
    WorkflowVersionRule rows, and every NodeToolBinding is materialized into
    WorkflowVersionTool rows, so the snapshot is the complete executable
    configuration (rules and tools), not rules-only. This is unconditional so
    existing callers (autobuild, rollout approval) get full detail for free.
    """
    reason = (reason or "")[:64]

    with transaction.atomic():
        workflow = Workflow.objects.select_for_update().get(pk=workflow.pk)

        current = _current_slots(workflow)
        current_set = {node_key: wb.id for node_key, wb in current.items()}

        latest = _latest_version(workflow)
        if latest is not None:
            latest_set = {s.node_key: s.workbench_id for s in latest.slots.all()}
            if not force and latest_set == current_set:
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

        from builder.bindings_sync import list_workflow_rules

        rule_dicts = list_workflow_rules(workflow)
        if rule_dicts:
            WorkflowVersionRule.objects.bulk_create([
                WorkflowVersionRule(
                    workflow_version=snapshot,
                    shape_id=r["shape_id"],
                    shape_label=r["shape_label"],
                    workbench_id=r["workbench_id"],
                    node_key=r["node_key"],
                    rule_key=r["rule_key"],
                    is_custom=r["is_custom"],
                    condition=r["condition"],
                    action=r["action"],
                    decision_type=r["decision_type"],
                    codes=r["codes"],
                    subrule_id=r["subrule_id"],
                    sop_id=r["sop_id"],
                    sop_title=r["sop_title"],
                    sop_version_number=r["sop_version_number"],
                    references_json=r["references_json"],
                    excluded_by_json=r["excluded_by_json"],
                    html_reference_json=r["html_reference_json"],
                    orphaned_from_rule_key=r["orphaned_from_rule_key"],
                    orphaned_from_sop_id=r["orphaned_from_sop_id"],
                    orphaned_reason=r["orphaned_reason"],
                    ordering=r["ordering"],
                )
                for r in rule_dicts
            ])

        from builder.bindings_sync import list_workflow_tools

        tool_dicts = list_workflow_tools(workflow)
        if tool_dicts:
            WorkflowVersionTool.objects.bulk_create([
                WorkflowVersionTool(
                    workflow_version=snapshot,
                    shape_id=t["shape_id"],
                    workbench_id=t["workbench_id"],
                    node_key=t["node_key"],
                    tool_id=t["tool_id"],
                    tool_name=t["tool_name"],
                    rule_key=t["rule_key"],
                    args_template=t["args_template"],
                    ordering=t["ordering"],
                )
                for t in tool_dicts
            ])

        return snapshot
