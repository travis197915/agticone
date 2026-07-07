"""Collapse duplicate ``is_current`` AuditSop rows down to one per URL.

Because the AuditSop unique constraint is on ``(job, content_hash)``, every
re-ingest of the same document inserts a NEW row with ``is_current=True``. When
the version registry failed to supersede the prior rows (e.g. older ingests left
``canonical_url`` empty so the dedup never matched), a single SOP URL ends up
with many ``is_current`` rows. That breaks the version lookup and leaves
auto-build unable to pick a single SOP.

This command groups current rows by URL and keeps exactly ONE — the best row
(most AuditSteps, then most AuditDecisions, then newest id) — demoting the rest
to ``is_current=False`` / ``activation_status='superseded'``. It also backfills
an empty ``canonical_url`` from the row's ``url``.

Usage::

    python manage.py dedupe_current_sops              # apply
    python manage.py dedupe_current_sops --dry-run     # preview only
    python manage.py dedupe_current_sops --url
"""

from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from sop_ingestion.models import AuditDecision, AuditSop, AuditStep

try:
    from uhc_sop_ingestion.revision import normalize_canonical_url
except Exception:  # pragma: no cover - fallback if package layout differs

    def normalize_canonical_url(u: str) -> str:
        return (u or "").strip().rstrip("/")


def _url_key(sop: AuditSop) -> str:
    base = (sop.canonical_url or "").strip() or (sop.url or "").strip()
    return normalize_canonical_url(base).lower() or f"sop:{sop.id}"


def _score(sop: AuditSop) -> tuple:
    steps = AuditStep.objects.filter(sop=sop).count()
    decisions = AuditDecision.objects.filter(step__sop=sop).count()
    return (steps, decisions, sop.id)


class Command(BaseCommand):
    help = "Keep one is_current AuditSop per URL; supersede duplicates."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report changes without writing them.",
        )
        parser.add_argument(
            "--url", default=None, help="Limit to a single URL (canonical_url or url)."
        )

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        only_url = (opts.get("url") or "").strip().rstrip("/").lower() or None

        qs = AuditSop.objects.filter(is_current=True)
        groups: dict[str, list[AuditSop]] = defaultdict(list)
        for sop in qs:
            key = _url_key(sop)
            if only_url and key != only_url:
                continue
            groups[key].append(sop)

        total_demoted = 0
        total_backfilled = 0
        dup_groups = 0

        for key, sops in sorted(groups.items()):
            # Backfill empty canonical_url from url regardless of duplication.
            for s in sops:
                if not (s.canonical_url or "").strip() and (s.url or "").strip():
                    total_backfilled += 1
                    if not dry:
                        s.canonical_url = normalize_canonical_url(s.url)
                        s.save(update_fields=["canonical_url", "updated_at"])

            if len(sops) <= 1:
                continue
            dup_groups += 1
            keep = max(sops, key=_score)
            losers = [s for s in sops if s.id != keep.id]
            self.stdout.write(
                f"{key}\n  KEEP  #{keep.id} score={_score(keep)}\n"
                f"  DROP  {[s.id for s in losers]}"
            )
            total_demoted += len(losers)
            if not dry:
                with transaction.atomic():
                    AuditSop.objects.filter(id__in=[s.id for s in losers]).update(
                        is_current=False, activation_status="superseded"
                    )

        verb = "Would demote" if dry else "Demoted"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{verb} {total_demoted} duplicate current row(s) across "
                f"{dup_groups} URL group(s); backfilled {total_backfilled} "
                f"canonical_url value(s). {'(dry-run)' if dry else ''}"
            )
        )
