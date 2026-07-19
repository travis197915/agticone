#!/usr/bin/env python3
r"""READ-ONLY prod check: where does "not found" still appear on a claim run?

Why
---
Auditors flagged coverage lines that say the CPT was "not found", which reads
like a contradiction against the CBD "covered" signal even though every claim is
CLEAN. We scrub the COVERAGE "not found" wording to "covered under the applicable
CBD benefit" (see ``enrich_coverage_determination.py``). But "not found" also
appears in TWO UNRELATED, LEGITIMATE contexts that must NOT be rewritten:

  * PROVIDER  — "individual provider record not found in facets claim summary …"
  * DOC360    — "404 'document not found'" image-lookup notes

So a blanket replace would be wrong. This script writes NOTHING — it just shows
every "not found" occurrence on the workflow's executive summaries (and,
optionally, stored traces), classifies each as COVERAGE / PROVIDER / DOC360 /
OTHER, and prints per-claim context so you can confirm what the scrub should and
should not touch.

Environment / usage: same prod defaults as ``enrich_coverage_determination.py``
(prod PG/Mongo baked in via setdefault; ANY real env var overrides; no LLM).

    # whole workflow, executive summaries only (default)
    python scripts/check_coverage_not_found_prod.py

    # one claim, include stored traces too
    python scripts/check_coverage_not_found_prod.py --claim 25XK20940100 --include-traces

    # only show the coverage-classified hits (the ones the scrub targets)
    python scripts/check_coverage_not_found_prod.py --only coverage
"""
from __future__ import annotations

import argparse
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _find_repo_root(start: str) -> str:
    d = start
    for _ in range(6):
        if os.path.exists(os.path.join(d, "manage.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return start


REPO_ROOT = _find_repo_root(_HERE)

DEFAULT_WORKFLOW_ID = "7c476f09-5196-438f-b25e-9cc3c96eac97"

_PROD_ENV = {
    "APP_ENV": "prod",
    "DJANGO_SETTINGS_MODULE": "sop_backend.settings",
    "LLM_BACKEND": "none",
    "PG_HOST": "azure-pgsql-flexibleserver-np-390744103630-dev.privatelink.postgres.database.azure.com",
    "PG_PORT": "5432",
    "PG_USER": "pgazdev",
    "PG_PASSWORD": "Xudzab-doxsoz-1vudra",
    "PG_DATABASE": "uhc_backend",
    "MONGO_URI": (
        "mongodb+srv://admin:s1Hd6lsd34jdkkljsdnflssd99ef8dfsdfc2Gl1Uu"
        "@mongoatlasagentic-pl-1.1r9aua.mongodb.net/"
        "?retryWrites=true&w=majority&serverSelectionTimeoutMS=30000"
        "&connectTimeoutMS=30000"
    ),
    "MONGO_DATABASE": "sop_ingestion_v2",
}


def _bootstrap_django() -> None:
    for key, val in _PROD_ENV.items():
        os.environ.setdefault(key, val)
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    import django

    django.setup()


_NF = re.compile(r"not[ \-]?found", re.I)
_CTX = re.compile(r".{0,60}not[ \-]?found.{0,60}", re.I)


def _classify(snippet: str) -> str:
    """Bucket a 'not found' snippet by context."""
    s = snippet.lower()
    if "document not found" in s or "404" in s or "doc360" in s or "doc 360" in s:
        return "DOC360"
    if "provider record" in s or "claim summary" in s or "prpr_enti" in s:
        return "PROVIDER"
    if any(k in s for k in (
        "coverage", "medicare coverage", "cbd", "covered benefit",
        "coverage database", "coverage records", "not_found_codes", "procedure code",
    )):
        return "COVERAGE"
    return "OTHER"


def _iter_fields(es):
    """Yield (field_label, text) for every text field on an exec summary."""
    yield "headline", es.headline or ""
    yield "overall_summary", es.overall_summary or ""
    for i, k in enumerate(es.key_findings or []):
        yield f"key_findings[{i}]", str(k)
    for i, s in enumerate(es.step_summaries or []):
        if isinstance(s, dict):
            yield f"step[{i}] {s.get('agent_name','')[:40]}", str(s.get("summary") or "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--claim", default="")
    ap.add_argument("--only", choices=["coverage", "provider", "doc360", "other", "all"],
                    default="all", help="Show only hits of this class.")
    ap.add_argument("--include-traces", action="store_true",
                    help="Also scan stored ClaimTrace sub-rule statements.")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show-clean", action="store_true",
                    help="Also list claims with zero hits.")
    opts = ap.parse_args(argv)
    only = opts.only.upper()

    _bootstrap_django()
    from execution_app.models import ClaimExecutiveSummary, ClaimTrace

    qs = ClaimExecutiveSummary.objects.filter(run__workflow_id=opts.workflow)
    if opts.claim:
        qs = qs.filter(claim_id=opts.claim)
    qs = qs.order_by("claim_id")
    if opts.limit:
        qs = qs[: opts.limit]
    total = qs.count()

    print("=" * 78)
    print("READ-ONLY check: 'not found' occurrences on executive summaries")
    print(f"  workflow      : {opts.workflow}")
    print(f"  claim filter  : {opts.claim or '(all)'}")
    print(f"  class filter  : {only}")
    print(f"  exec summaries: {total}")
    print("=" * 78)

    by_class: dict[str, int] = {"COVERAGE": 0, "PROVIDER": 0, "DOC360": 0, "OTHER": 0}
    claims_with_coverage: list[str] = []
    scanned = 0

    for es in qs.iterator():
        scanned += 1
        hits = []
        claim_has_coverage = False
        for label, text in _iter_fields(es):
            if not _NF.search(text):
                continue
            for snip in _CTX.findall(text):
                cls = _classify(snip)
                by_class[cls] = by_class.get(cls, 0) + 1
                if cls == "COVERAGE":
                    claim_has_coverage = True
                if only != "ALL" and cls != only:
                    continue
                hits.append((cls, label, re.sub(r"\s+", " ", snip.strip())))
        if claim_has_coverage:
            claims_with_coverage.append(es.claim_id)

        if hits:
            print(f"\n● claim {es.claim_id}  (verdict={es.verdict}, status={es.audit_status})")
            for cls, label, snip in hits:
                print(f"    [{cls:8}] {label}")
                print(f"               …{snip}…")
        elif opts.show_clean:
            print(f"○ claim {es.claim_id}: no 'not found'")

    # Optional: stored trace sub-rule statements.
    trace_cov = 0
    if opts.include_traces:
        tq = ClaimTrace.objects.filter(run__workflow_id=opts.workflow)
        if opts.claim:
            tq = tq.filter(claim_id=opts.claim)
        print("\n" + "-" * 78)
        print("Stored ClaimTrace scan (statement/label fields):")
        for t in tq.iterator():
            blob = ""
            for coll in (t.trace_json, t.explainability_json):
                if isinstance(coll, (list, dict)):
                    blob += str(coll)
            for snip in _CTX.findall(blob):
                if _classify(snip) == "COVERAGE":
                    trace_cov += 1
                    if opts.claim:
                        clean = re.sub(r"\s+", " ", snip.strip())
                        print(f"    {t.claim_id}: …{clean}…")
        print(f"  coverage 'not found' snippets still in traces: {trace_cov}")
        print("  (coarse: counts the agent's raw REASONING text too, which may")
        print("   legitimately say \"'not found' is a valid coverage category\";")
        print("   the auditor-facing fields are the exec summary + RULE-003/005")
        print("   determination statement.)")

    print("\n" + "=" * 78)
    print(f"Scanned {scanned} executive summaries.")
    print("Occurrences by class (across all fields):")
    for cls in ("COVERAGE", "PROVIDER", "DOC360", "OTHER"):
        print(f"    {cls:8}: {by_class.get(cls, 0)}")
    cov_claims = sorted(set(claims_with_coverage))
    print(f"\nClaims with COVERAGE 'not found' still present: {len(cov_claims)}")
    if cov_claims:
        print("    " + ", ".join(cov_claims[:50])
              + (" …" if len(cov_claims) > 50 else ""))
    print("\nNOTE: only COVERAGE hits should be scrubbed. PROVIDER + DOC360 are")
    print("legitimate 'not found' notes and must be left as-is.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
