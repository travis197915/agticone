"""Triage tool for empty / suspicious RuleExecutionRun rows.

Splits rows into three buckets so the "no children in execution_rule_evaluation"
symptom can be classified:

  1. Totals by status (sanity check).
  2. Legitimate empty: status=FAILED with the load_bindings short-circuit
     message ("workflow has no attached rules"). These rows correctly have
     no children — there were no rules bound.
  3. Suspicious: status=COMPLETED with zero RuleEvaluation children, OR
     error_message starting with "persist:". Shows the per-workflow
     NodeRuleBinding count so the operator can tell "no rules bound" from
     "persistence dropped them".
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone


class Command(BaseCommand):
    help = "Audit RuleExecutionRun rows for missing/empty RuleEvaluation children."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--batch", type=str, default=None,
                            help="Restrict to one BatchExecutionRun id (UUID).")
        parser.add_argument("--since", type=int, default=24,
                            help="Look back N hours (default 24).")

    def handle(self, *_args, **opts) -> None:
        from agent_tools.models import NodeRuleBinding
        from execution_app.models import RuleExecutionRun

        since_hours = opts["since"]
        batch_id = opts["batch"]

        qs = RuleExecutionRun.objects.annotate(
            ev_count=Count("evaluations", distinct=True),
            tool_count=Count("tool_invocations", distinct=True),
        )
        if since_hours:
            cutoff = timezone.now() - timedelta(hours=since_hours)
            qs = qs.filter(started_at__gte=cutoff)
        if batch_id:
            try:
                qs = qs.filter(batch_id=uuid.UUID(batch_id))
            except ValueError as exc:
                raise CommandError(f"--batch must be a UUID: {exc}") from exc

        total = qs.count()
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Audited {total} RuleExecutionRun rows "
            f"(since {since_hours}h, batch={batch_id or 'ANY'})"))

        # 1. Totals by status
        self.stdout.write("\nTotals by status:")
        by_status = (qs.values("status")
                       .annotate(n=Count("id"))
                       .order_by("-n"))
        for row in by_status:
            self.stdout.write(f"  {row['status']:<18} {row['n']}")

        # 2. Legitimate empty
        legit = qs.filter(
            status="FAILED",
            error_message__startswith="workflow has no attached rules",
        )
        self.stdout.write(self.style.SUCCESS(
            f"\nLegitimate empty (no rules bound): {legit.count()}"))

        # 3. Suspicious — either COMPLETED with zero children, or a
        #    surfaced persist failure.
        suspicious = qs.filter(
            ev_count=0,
            status__in=["COMPLETED", "TERMINATED_EARLY"],
        ) | qs.filter(error_message__startswith="persist:")
        suspicious = suspicious.distinct().order_by("-started_at")

        self.stdout.write(self.style.WARNING(
            f"\nSuspicious rows: {suspicious.count()}"))
        if not suspicious.exists():
            return

        bindings_by_workflow: dict[str, int] = {}
        wf_ids = {r.workflow_id for r in suspicious}
        for wf_id in wf_ids:
            bindings_by_workflow[str(wf_id)] = NodeRuleBinding.objects.filter(
                shape__workbench__work_area__workflow_id=wf_id,
            ).count()

        self.stdout.write(
            f"  {'id':<38} {'claim_id':<20} {'status':<18} {'evs':<4} "
            f"{'binds':<6} finished_at  error_message")
        for r in suspicious[:200]:
            bind_n = bindings_by_workflow.get(str(r.workflow_id), 0)
            err = (r.error_message or "").replace("\n", " ")[:120]
            finished = (r.finished_at.isoformat()
                        if r.finished_at else "—")
            self.stdout.write(
                f"  {str(r.id):<38} {r.claim_id[:20]:<20} "
                f"{r.status:<18} {r.ev_count:<4} {bind_n:<6} "
                f"{finished}  {err}")
        if suspicious.count() > 200:
            self.stdout.write(
                f"  … {suspicious.count() - 200} more rows truncated")
