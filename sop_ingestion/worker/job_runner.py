#!/usr/bin/env python
"""Subprocess entrypoint: run one ingestion job by job_id.

Usage (from repo root, PYTHONPATH=.):

    python -m sop_ingestion.worker.job_runner <job_id>

The Celery master task spawns this script; it is not invoked by hand in normal ops.
"""
from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [job_runner] %(levelname)s %(message)s",
)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m sop_ingestion.worker.job_runner <job_id>", file=sys.stderr)
        return 2

    job_id = sys.argv[1].strip()
    if not job_id:
        print("job_id is required", file=sys.stderr)
        return 2

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sop_backend.settings")

    import django

    django.setup()

    from sop_ingestion.pipeline_runner import execute_ingestion_job

    result = execute_ingestion_job(job_id)
    if result.get("error"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
