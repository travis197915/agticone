"""Generate ClaimExecutiveSummary rows for existing runs.

Runs the same generator the n08 ``executive_summary`` add-on agent uses, but
offline over history — so claims that were audited before the add-on existed
get a concise human-auditor summary without re-running the engine.

Idempotent: skips runs that already have a summary unless ``--force``. LLM
calls are the slow part, so generation is parallelized across ``--workers``.
Set ``NO_LLM=1`` to write deterministic (non-LLM) summaries fast.

Usage:
    PYTHONPATH=. python manage.py backfill_executive_summary
    PYTHONPATH=. python manage.py backfill_executive_summary --claim-id 123 --force
    PYTHONPATH=. python manage.py backfill_executive_summary --workers 8 --limit 50
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.management.base import BaseCommand
from django.db import close_old_connections, connection

from execution_app.executive_summary import generate_for_run
from execution_app.models import ClaimExecutiveSummary, RuleExecutionRun

_TERMINAL = ("COMPLETED", "TERMINATED_EARLY", "FAILED")


class Command(BaseCommand):
    help = "Backfill ClaimExecutiveSummary rows for existing execution runs."

    def add_arguments(self, parser):
        parser.add_argument("--claim-id", default="",
                            help="Only this claim id.")
        parser.add_argument("--run-id", default="",
                            help="Only this run id (UUID).")
        parser.add_argument("--limit", type=int, default=0,
                            help="Cap the number of runs processed (0 = all).")
        parser.add_argument("--workers", type=int, default=6,
                            help="Parallel generation workers (default 6).")
        parser.add_argument("--force", action="store_true",
                            help="Regenerate even if a summary already exists.")
        parser.add_argument("--latest-per-claim", action="store_true",
                            help="Only the most recent run for each claim id.")

    def handle(self, *args, **opts):
        qs = RuleExecutionRun.objects.filter(status__in=_TERMINAL)
        if opts["claim_id"]:
            qs = qs.filter(claim_id=opts["claim_id"])
        if opts["run_id"]:
            qs = qs.filter(id=opts["run_id"])
        if not opts["force"]:
            have = set(
                ClaimExecutiveSummary.objects.values_list("run_id", flat=True)
            )
        else:
            have = set()

        run_ids: list[str] = []
        seen_claims: set[str] = set()
        for rid, claim_id in qs.order_by("-started_at").values_list("id", "claim_id"):
            if not opts["force"] and rid in have:
                continue
            if opts["latest_per_claim"]:
                key = claim_id or str(rid)
                if key in seen_claims:
                    continue
                seen_claims.add(key)
            run_ids.append(str(rid))
            if opts["limit"] and len(run_ids) >= opts["limit"]:
                break

        total = len(run_ids)
        if total == 0:
            self.stdout.write(self.style.WARNING("Nothing to backfill."))
            return

        self.stdout.write(
            f"Generating executive summaries for {total} run(s) "
            f"with {opts['workers']} worker(s)…"
        )

        force = opts["force"]
        ok = 0
        skipped = 0
        failed = 0

        def _work(run_id: str) -> tuple[str, str]:
            try:
                run = RuleExecutionRun.objects.filter(id=run_id).first()
                if run is None:
                    return run_id, "skip"
                row = generate_for_run(run, source="backfill", force=force)
                return run_id, ("ok" if row is not None else "skip")
            except Exception as exc:  # noqa: BLE001 - report, keep going
                return run_id, f"fail:{exc}"
            finally:
                close_old_connections()

        workers = max(1, opts["workers"])
        if workers == 1:
            results = (_work(rid) for rid in run_ids)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_work, rid) for rid in run_ids]
                results = (f.result() for f in as_completed(futures))

        done = 0
        for run_id, outcome in results:
            done += 1
            if outcome == "ok":
                ok += 1
            elif outcome == "skip":
                skipped += 1
            else:
                failed += 1
                self.stderr.write(f"  run={run_id} {outcome}")
            if done % 20 == 0 or done == total:
                self.stdout.write(f"  {done}/{total} (ok={ok} skip={skipped} fail={failed})")

        connection.close()
        self.stdout.write(self.style.SUCCESS(
            f"Done. ok={ok} skipped={skipped} failed={failed} of {total}."
        ))
