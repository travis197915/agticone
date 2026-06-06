"""One-off driver: ingest every POC HTML through the full LangGraph pipeline.

Creates a proper Django IngestionJob per POC (so Postgres job tracking, Mongo,
Neo4j, Redis all get exercised) and runs execute_ingestion_job in-process.

Run:
    PYTHONPATH=. python poc/_ingest_pocs.py
"""
from __future__ import annotations

import os
import sys
import json
import time
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sop_backend.settings")

import django  # noqa: E402

django.setup()

from sop_ingestion.models import IngestionJob  # noqa: E402
from sop_ingestion.pipeline_runner import execute_ingestion_job  # noqa: E402

POC_ROOT = Path(__file__).resolve().parent

POCS = [
    "claims_spanning_eligibility_dates",
    "duplicate_claim_handling",
    "obh_facets_timely_filing",
    "physician_claim_checklist",
    "provider_selection_guidelines",
    "timely_filing_050526",
]


def main() -> int:
    results = []
    for name in POCS:
        html = POC_ROOT / name / "html" / "index.html"
        if not html.exists():
            print(f"!! MISSING {html}", flush=True)
            results.append({"poc": name, "error": "missing html"})
            continue

        job = IngestionJob.objects.create(
            seed_url=str(html),
            max_depth=0,       # single document, no crawl to css / cross-links
            max_docs=1,
            llm_provider="anthropic",
            llm_model="claude-sonnet-4-5-20250929",
        )
        print(f"\n===== INGEST {name}  job={job.job_id} =====", flush=True)
        t0 = time.time()
        out = execute_ingestion_job(str(job.job_id))
        dt = time.time() - t0

        job.refresh_from_db()
        rec = {
            "poc": name,
            "job_id": str(job.job_id),
            "status": job.status,
            "docs_processed": job.docs_processed,
            "docs_failed": job.docs_failed,
            "llm_calls": job.total_llm_calls,
            "tokens_in": job.total_tokens_in,
            "tokens_out": job.total_tokens_out,
            "seconds": round(dt, 1),
            "summary": job.summary,
            "n_errors": len(job.errors or []),
        }
        results.append(rec)
        print(json.dumps(rec, indent=2, default=str), flush=True)

    print("\n\n========== FINAL SUMMARY ==========", flush=True)
    print(json.dumps(results, indent=2, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
