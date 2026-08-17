"""Backfill exactly one WorkflowVersion per existing Workflow.

Deliberately conservative: creates ONE snapshot per workflow, at its
*current* Workbench composition and *current* Workflow.version value — never
fabricates the composition for version_number 1..N-1 of a workflow already
past v1, since the database cannot prove what those historical combinations
actually were (see builder.workflow_versioning module docstring / the
approved implementation plan). Workflows created after this migration get
their full version history recorded going forward by
snapshot_workflow_version, called from builder.sop_autobuild and
sop_ingestion.services.rule_changes.
"""
from __future__ import annotations

import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def _current_slots(Workbench, workflow) -> list:
    """One Workbench per node_key — see builder.workflow_versioning._current_slots
    for why this dedupes rather than trusting is_current=True to be unique per
    node_key (it is not enforced at the DB level, and real data on this branch
    has already been observed to violate it)."""
    seen: dict[str, object] = {}
    qs = Workbench.objects.filter(
        work_area__workflow=workflow, is_current=True,
    ).order_by("-version", "-updated_at")
    for wb in qs:
        if wb.node_key not in seen:
            seen[wb.node_key] = wb
    return list(seen.values())


def backfill(apps, schema_editor):
    Workflow = apps.get_model("builder", "Workflow")
    Workbench = apps.get_model("builder", "Workbench")
    WorkflowVersion = apps.get_model("builder", "WorkflowVersion")
    WorkflowVersionWorkbench = apps.get_model("builder", "WorkflowVersionWorkbench")
    AuditSop = apps.get_model("sop_ingestion", "AuditSop")

    for workflow in Workflow.objects.all().iterator():
        current = _current_slots(Workbench, workflow)
        if not current:
            continue  # nothing built yet for this workflow — no snapshot to make

        snapshot = WorkflowVersion.objects.create(
            workflow=workflow, version_number=workflow.version,
            reason="backfill",
        )

        sop_ids = {
            (wb.config or {}).get("sop_id")
            for wb in current
            if (wb.config or {}).get("sop_id")
        }
        sop_lookup = {
            row["id"]: row
            for row in AuditSop.objects.filter(id__in=sop_ids).values(
                "id", "title", "version_number",
            )
        } if sop_ids else {}

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
            for wb in current
        ])


def noop_reverse(apps, schema_editor):
    # Nothing to reverse to — the pre-migration state had no WorkflowVersion
    # rows at all, so reversing just deletes what this migration created.
    WorkflowVersion = apps.get_model("builder", "WorkflowVersion")
    WorkflowVersion.objects.filter(reason="backfill").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("builder", "0007_workflow_version_snapshot"),
        ("sop_ingestion", "0030_remove_rulechangeproposal_uniq_proposal_per_rule_per_change_set_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, noop_reverse),
    ]
