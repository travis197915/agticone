"""Crawl HTML SOPs with BeautifulSoup and cache them in MongoDB.

Only HTTP(S) HTML SOPs are crawled; PDF uploads and node/YAML SOPs are skipped.

    PYTHONPATH=. python manage.py crawl_sop_html            # all current SOPs
    PYTHONPATH=. python manage.py crawl_sop_html --sop-id 12
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from sop_ingestion.models import AuditSop
from sop_ingestion.sop_html_crawler import crawl_and_store, is_crawlable_url


class Command(BaseCommand):
    help = "Crawl HTML SOPs (BeautifulSoup) and store the HTML in MongoDB. PDFs ignored."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sop-id", type=int, default=None,
            help="Crawl only this AuditSop id (default: all current SOPs).",
        )

    def handle(self, *args, **opts):
        qs = AuditSop.objects.filter(is_current=True)
        if opts.get("sop_id"):
            qs = qs.filter(pk=opts["sop_id"])

        stored = skipped = failed = 0
        for sop in qs.order_by("id"):
            url = sop.url or ""
            if not is_crawlable_url(url):
                self.stdout.write(f"skip   #{sop.id}  ({url[:70] or 'no-url'}) — not HTML")
                skipped += 1
                continue
            try:
                doc = crawl_and_store(sop)
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"error  #{sop.id}  {exc}"))
                failed += 1
                continue
            if doc and doc.get("html"):
                self.stdout.write(self.style.SUCCESS(
                    f"stored #{sop.id}  {doc['page_count']} page(s)  "
                    f"{doc['byte_size']:,}B  — {(sop.title or '')[:50]}"
                ))
                stored += 1
            else:
                self.stdout.write(self.style.WARNING(f"empty  #{sop.id}  — nothing crawled"))
                failed += 1

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Done. stored={stored} skipped={skipped} failed={failed}"
        ))
