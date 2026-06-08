"""Re-apply the (corrected) decision classifier to existing AuditDecision rows.

The SOP importer used to stamp routing language ("proceed to next step", "skip
to step N", "out of scope") as REFER/STOP dispositions, which produced false
DEFECT/REFER verdicts on clean claims. The classifier in ``import_sop_yaml`` is
now fixed, but rows already in the database keep their stale ``decision_type``.

Re-importing would recreate the AuditStep/AuditDecision rows (new PKs) and break
every NodeRuleBinding / NodeToolBinding that points at them, so instead we
recompute ``decision_type`` in place from each row's stored text. Bindings,
row indexes and PKs are all preserved.

Usage::

    python manage.py reclassify_decisions            # apply to all SOPs
    python manage.py reclassify_decisions --dry-run   # preview only
    python manage.py reclassify_decisions --sop 2 5 6 9
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from sop_ingestion.models import AuditDecision
from sop_ingestion.management.commands.import_sop_yaml import _classify_decision


class Command(BaseCommand):
    help = "Recompute AuditDecision.decision_type in place using the fixed classifier."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report changes without writing them.")
        parser.add_argument("--sop", nargs="*", default=None,
                            help="Limit to these SOP ids (default: all).")

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        sop_ids = opts.get("sop")

        qs = AuditDecision.objects.select_related("step", "step__sop").all()
        if sop_ids:
            qs = qs.filter(step__sop_id__in=sop_ids)

        transitions: Counter = Counter()
        changed: list[tuple[str, str, str]] = []

        with transaction.atomic():
            for d in qs.iterator():
                blob = " ".join(filter(None, [
                    d.action_text or "",
                    d.condition_if or "",
                    d.output_text or "",
                ]))
                new_type = _classify_decision(blob)
                if new_type != d.decision_type:
                    key = f"step:{d.step.sop_id}:{d.step.step_number}:{d.row_index}"
                    transitions[(d.decision_type, new_type)] += 1
                    changed.append((key, d.decision_type, new_type))
                    if not dry:
                        d.decision_type = new_type
                        d.save(update_fields=["decision_type"])
            if dry:
                transaction.set_rollback(True)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"{'[dry-run] ' if dry else ''}Reclassified {len(changed)} decision rows"
        ))
        for (old, new), n in sorted(transitions.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {old:>11} -> {new:<11} : {n}")
        if changed:
            self.stdout.write("\nExamples:")
            for key, old, new in changed[:25]:
                self.stdout.write(f"  {key:<22} {old} -> {new}")
