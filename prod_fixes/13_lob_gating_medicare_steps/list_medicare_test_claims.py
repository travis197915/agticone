#!/usr/bin/env python3
r"""List claim ids (per LOB scenario) to test the Medicare-only SOP scoping fix.

READ-ONLY. NO LLM. NO writes. Prints, for the audit workflow:

  * MEDICARE claims            — NPI/Opt-Out SHOULD still run (control)
  * NON-MEDICARE, clean ALLOW  — NPI/Opt-Out shown skipped / out-of-scope
  * NON-MEDICARE + real defect — skipped, but a genuine in-scope disposition

DB target defaults to PROD Postgres (baked-in below; any PG_* env var
overrides — the local prod-replica on 127.0.0.1:5433 works too).

Usage (prod box):
    python scripts/list_medicare_test_claims.py
    python scripts/list_medicare_test_claims.py --per-bucket 15

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend \
    python scripts/list_medicare_test_claims.py
"""
from __future__ import annotations

import argparse
import os
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

# Baked-in PROD Postgres (overridable by any PG_* env var already set).
_PROD_ENV = {
    "APP_ENV": "prod",
    "DJANGO_SETTINGS_MODULE": "sop_backend.settings",
    "LLM_BACKEND": "none",
    "NO_LLM": "1",
    "PG_HOST": "azure-pgsql-flexibleserver-np-390744103630-dev.privatelink.postgres.database.azure.com",
    "PG_PORT": "5432",
    "PG_USER": "pgazdev",
    "PG_PASSWORD": "Xudzab-doxsoz-1vudra",
    "PG_DATABASE": "uhc_backend",
}

_ADVERSE = {"DENY", "STOP", "REFER", "REFERRAL", "PEND", "PENDED"}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="List test claim ids per LOB scenario (read-only, no LLM)."
    )
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument(
        "--per-bucket", type=int, default=10, help="Max ids to print per bucket."
    )
    opts = ap.parse_args()

    for key, val in _PROD_ENV.items():
        os.environ.setdefault(key, val)
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    import django

    django.setup()

    from execution_app.models import RuleExecutionRun

    print(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    print(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    print(f"  workflow    = {opts.workflow}")

    latest: dict[str, RuleExecutionRun] = {}
    for run in RuleExecutionRun.objects.filter(workflow_id=opts.workflow).order_by(
        "claim_id", "-started_at"
    ):
        if run.claim_id and run.claim_id not in latest:
            latest[run.claim_id] = run

    medicare, clean, adverse = [], [], []
    for cid, run in latest.items():
        lob = run.claim_lob or {}
        product = (lob.get("product") or "").strip()
        label = lob.get("label") or product or "unknown"
        verdict = (run.final_decision_type or "").upper()
        row = (cid, label, verdict)
        if product == "Medicare":
            medicare.append(row)
        elif verdict in _ADVERSE:
            adverse.append(row)
        else:
            clean.append(row)

    def show(title: str, rows: list) -> None:
        print(f"\n=== {title} ({len(rows)} total) ===")
        for cid, label, verdict in sorted(rows)[: opts.per_bucket]:
            print(f"   {cid}   {label:<16} verdict={verdict}")

    show("MEDICARE — NPI/Opt-Out SHOULD run (control)", medicare)
    show("NON-MEDICARE, clean ALLOW — NPI/Opt-Out skipped", clean)
    show("NON-MEDICARE + real defect — skipped, genuine disposition", adverse)
    print(f"\n  total claims in workflow = {len(latest)}")


if __name__ == "__main__":
    main()
