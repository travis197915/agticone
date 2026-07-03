"""Rebuild ClaimMemory rows from existing execution history.

Memory is scoped per (claim_id, sop_id). For every claim with at least one
successful run, the latest successful run's evaluations are grouped by their
rule binding's SOP and written as one memory row per SOP. Evaluations whose
binding is gone (SET_NULL) land on the shared ``sop_id=""`` row, as do all
tool results (ToolInvocationRecord does not persist a SOP attribution —
engine-written runs do, so organic updates self-correct).

Idempotent — re-running replaces what a prior backfill produced. Existing
drift logs are preserved.

Usage:
    PYTHONPATH=. python manage.py backfill_claim_memory [--claim-id X] [--dry-run]
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from execution_app.models import ClaimMemory, RuleExecutionRun

from uhc_execution_engine.memory import (_NARRATIVE_CAP, _REASONING_CAP,
                                         _RUN_HISTORY_CAP, payload_hash,
                                         sop_key, tool_memory_key)

_SUCCESS = ("COMPLETED", "TERMINATED_EARLY")


class Command(BaseCommand):
    help = "Backfill per-(claim, SOP) ClaimMemory rows from existing runs."

    def add_arguments(self, parser):
        parser.add_argument("--claim-id", default="",
                            help="Backfill only this claim id.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would be written; change nothing.")

    def handle(self, *args, **opts):
        from sop_ingestion.models import AuditSop

        qs = (RuleExecutionRun.objects
              .filter(status__in=_SUCCESS)
              .exclude(claim_id=""))
        if opts["claim_id"]:
            qs = qs.filter(claim_id=opts["claim_id"])

        claim_ids = qs.values_list("claim_id", flat=True).order_by().distinct()
        sop_titles = {}
        written = 0
        for claim_id in claim_ids:
            runs = list(
                RuleExecutionRun.objects
                .filter(claim_id=claim_id, status__in=_SUCCESS)
                .order_by("started_at")
            )
            if not runs:
                continue
            latest = runs[-1]

            # Group the latest run's evaluations by their binding's SOP.
            rules_by_sop: dict[str, dict] = {}
            for ev in (latest.evaluations
                       .select_related("rule_binding")
                       .order_by("order_index")):
                skey = sop_key(getattr(ev.rule_binding, "sop_id", None))
                rules_by_sop.setdefault(skey, {})[ev.rule_key] = {
                    "matched": ev.matched,
                    "skipped": ev.skipped,
                    "confidence": ev.confidence,
                    "reasoning": (ev.reasoning or "")[:_REASONING_CAP],
                    "decision_type": ev.decision_type or "",
                    "llm_status": "",
                    "navigation": None,
                    "run_id": str(latest.id),
                    "at": (latest.finished_at or latest.started_at).isoformat(),
                }

            # Tool results carry no SOP attribution in history -> shared row.
            # Reused records carry the reuse timestamp, not the live call's —
            # including them would restart the freshness TTL artificially.
            tool_memory = {}
            for inv in latest.tool_invocations.filter(
                    phase="EVALUATE", ok=True, reused_from_run__isnull=True):
                result = inv.result
                if not isinstance(result, (dict, list)):
                    result = {"value": result}
                tool_memory[tool_memory_key(inv.tool_name, inv.args or {})] = {
                    "ok": True,
                    "result": result,
                    "phase": "EVALUATE",
                    "run_id": str(latest.id),
                    "called_at": inv.called_at.isoformat(),
                }
            if tool_memory:
                rules_by_sop.setdefault("", {})

            if not rules_by_sop:
                continue

            run_history = [{
                "run_id": str(r.id),
                "batch_id": str(r.batch_id) if r.batch_id else "",
                "workflow_id": str(r.workflow_id),
                "status": r.status,
                "decision_type": r.final_decision_type or "",
                "codes": list(r.applied_codes or []),
                "finished_at": (r.finished_at or r.started_at).isoformat(),
            } for r in runs][-_RUN_HISTORY_CAP:]

            if opts["dry_run"]:
                self.stdout.write(
                    f"would write claim={claim_id} sops={sorted(rules_by_sop)} "
                    f"runs={len(runs)} tools={len(tool_memory)}")
                written += len(rules_by_sop)
                continue

            for skey, rule_memory in rules_by_sop.items():
                if skey and skey not in sop_titles:
                    sop = AuditSop.objects.filter(id=skey).first()
                    sop_titles[skey] = (sop.title or f"SOP #{skey}") if sop else ""
                with transaction.atomic():
                    mem = (ClaimMemory.objects.select_for_update()
                           .filter(claim_id=claim_id, sop_id=skey)
                           .first())
                    if mem is None:
                        mem = ClaimMemory(claim_id=claim_id, sop_id=skey)
                    mem.sop_title = sop_titles.get(skey, "")
                    mem.runs_count = len(runs)
                    mem.last_run = latest
                    mem.last_decision_type = latest.final_decision_type or ""
                    mem.last_narrative = (latest.narrative or "")[:_NARRATIVE_CAP]
                    mem.claim_payload_hash = payload_hash(
                        latest.claim_payload or {})
                    mem.rule_memory = rule_memory
                    mem.tool_memory = tool_memory if skey == "" else {}
                    mem.run_history = run_history
                    mem.save()
                written += 1
            self.stdout.write(
                f"claim={claim_id}: sop rows={len(rules_by_sop)} "
                f"runs={len(runs)} tools={len(tool_memory)}")

        verb = "would write" if opts["dry_run"] else "wrote"
        self.stdout.write(self.style.SUCCESS(
            f"backfill_claim_memory: {verb} {written} memory row(s)"))
