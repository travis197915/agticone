"""One-off driver: ingest a single SOP URL end-to-end with SOP_IR_PERSIST on.

Creates a builder Workflow flagged for auto-build (so the canvas is visible in
the UI) + an IngestionJob FK'd to it, then runs the full LangGraph pipeline
in-process (Postgres + Mongo + Neo4j + Redis all exercised), persists the
canonical IR, and auto-builds the canvas.

Run:
    SOP_IR_PERSIST=1 PYTHONPATH=. python poc/_ingest_one.py <seed_url> ["Workflow Name"]
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sop_backend.settings")

import django  # noqa: E402

django.setup()

from builder.models import Workflow  # noqa: E402
from sop_ingestion.models import (  # noqa: E402
    AuditSop, AuditStep, AuditDecision, IngestionJob, SopIRDocument,
)
from sop_ingestion.pipeline_runner import execute_ingestion_job  # noqa: E402


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "workflow"
    slug, n = base, 1
    while Workflow.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


def main() -> int:
    seed_url = sys.argv[1] if len(sys.argv) > 1 else (
        "http://localhost:9001/obh_facets_timely_filing/html/updated.html")
    name = sys.argv[2] if len(sys.argv) > 2 else "Timely Filing (IR test run)"

    print(f"persist_flag SOP_IR_PERSIST={os.environ.get('SOP_IR_PERSIST')!r}", flush=True)

    wf = Workflow.objects.create(
        name=name,
        slug=_slug(name),
        metadata={"auto_build_canvas": True, "source_sop": seed_url},
    )
    job = IngestionJob.objects.create(
        workflow=wf,
        seed_url=seed_url,
        max_depth=0,
        max_docs=1,
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-5-20250929",
    )
    print(f"workflow={wf.id} slug={wf.slug}", flush=True)
    print(f"===== INGEST job={job.job_id} url={seed_url} =====", flush=True)

    t0 = time.time()
    out = execute_ingestion_job(str(job.job_id))
    dt = time.time() - t0

    job.refresh_from_db()
    wf.refresh_from_db()
    print("\n----- job result -----", flush=True)
    print(json.dumps({
        "job_id": str(job.job_id),
        "status": job.status,
        "docs_processed": job.docs_processed,
        "docs_failed": job.docs_failed,
        "llm_calls": job.total_llm_calls,
        "tokens_in": job.total_tokens_in,
        "tokens_out": job.total_tokens_out,
        "seconds": round(dt, 1),
        "n_errors": len(job.errors or []),
        "errors_head": (job.errors or [])[:5],
    }, indent=2, default=str), flush=True)

    print("\n----- DB projection -----", flush=True)
    sops = list(AuditSop.objects.filter(job=job).order_by("id"))
    for s in sops:
        n_steps = AuditStep.objects.filter(sop=s).count()
        n_dec = AuditDecision.objects.filter(step__sop=s).count()
        irdocs = list(SopIRDocument.objects.filter(sop=s).order_by("ir_version"))
        print(f"AuditSop #{s.id} {s.title!r} steps={n_steps} decisions={n_dec}", flush=True)
        for d in irdocs:
            print(f"   IRdoc v{d.ir_version} source={d.source} status={d.validation_status} "
                  f"hash={d.content_hash[:12]} mongo_ref={d.mongo_ref} "
                  f"rules={d.rule_count} steps={d.step_count} dec={d.decision_count} "
                  f"errs={len(d.validation_errors or [])}", flush=True)
        # routing-bearing rows
        routed = []
        for st in AuditStep.objects.filter(sop=s).order_by("step_number"):
            for dd in AuditDecision.objects.filter(step=st):
                goto = getattr(dd, "goto_step", None) or getattr(dd, "next_step", None)
                tags = []
                if goto:
                    tags.append(f"goto={goto}")
                if dd.is_out_of_scope:
                    tags.append("OOS")
                if getattr(dd, "is_final", False):
                    tags.append("FINAL")
                if dd.applicable_when:
                    tags.append(f"appl={dd.applicable_when!r}")
                if dd.aggregation and dd.aggregation != "LEAF":
                    tags.append(f"agg={dd.aggregation}")
                if tags:
                    routed.append(f"     step{st.step_number:>2} {st.yaml_rule_id or '':<9} "
                                  f"{(dd.subrule_id or dd.decision_type):<14} {' '.join(tags)}")
        if routed:
            print("   routing rows:", flush=True)
            print("\n".join(routed), flush=True)

    print("\n----- auto-build -----", flush=True)
    from builder.models import Shape
    shape_count = Shape.objects.filter(workbench__work_area__workflow=wf).count()
    meta = wf.metadata or {}
    print(json.dumps({
        "workflow_id": str(wf.id),
        "slug": wf.slug,
        "auto_build_complete": meta.get("auto_build_complete"),
        "auto_build_stats": meta.get("auto_build_stats"),
        "needs_tools": meta.get("needs_tools"),
        "shape_count": shape_count,
    }, indent=2, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
