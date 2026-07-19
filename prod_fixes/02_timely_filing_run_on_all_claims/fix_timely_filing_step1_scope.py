#!/usr/bin/env python3
r"""Stop the Timely Filing SOP from bailing at Step 1 so timely filing is
CONFIRMED on every claim (not only when a TF0/TF1 denial already exists).

The bug
-------
Step 1 asks "Is your claim/line denying for TF1 or TF0?":

    row0  Yes -> proceed to next step
    row1  No  -> "This P&P does not apply."

On a clean claim (no TF0/TF1 denial) the **No** row matches. That row carries
BOTH ``is_out_of_scope=True`` and ``is_final=True`` in the DB. In the execution
engine (``n_execute_shapes``) a matched out-of-scope row is the highest-precedence
"clean stop", and a matched is_final row is a terminal stop — either one ends the
whole SOP. So every downstream step (2-18) is stamped
"skipped: prior step out of scope — auditing stopped" and the dashboard paints
the entire SOP OUT OF SCOPE. The determination steps (state eligibility,
deadline calc, days-elapsed, limit comparison) never run.

The YAML spec (yaml/Timely_Filing.yaml, RULE-001) keeps Step 1 fully IN SCOPE —
it never marks itself out-of-scope/terminal. This is the ONLY out-of-scope
discrepancy between the YAML and the DB; the 8 intentionally out-of-scope steps
(6, 7, 8, 9, 13, 14, 15, 16) already match.

What this does
--------------
For the Timely Filing SOP's Step 1, on the "does not apply" branch it:
  * clears ``is_out_of_scope`` and ``is_final`` on the AuditDecision row, and
  * rewrites its action from "This P&P does not apply." to "Proceed to the next
    step to verify timely filing." on both the AuditDecision and the
    NodeRuleBinding the engine executes.

Result: a matched **No** falls through to normal sequential routing -> Step 2,
so the in-scope determination (Steps 1, 2, 3, 4, 5, 10, 11, 12, 17) runs on every
claim. The genuinely out-of-scope steps (6-9 POTF-document, 13-16 manual/system
actions) are untouched.

Idempotent: re-running is a no-op once the flags are cleared.

DB target = hardcoded PROD Postgres by default (see HARDCODED_PG). Any PG_* env
var overrides it, so target local by exporting PG_HOST/PG_PORT/etc. The banner
ALWAYS prints the resolved target first; use --dry-run to preview, --apply to
commit.

    # prod (bare run uses the hardcoded creds)
    python scripts/fix_timely_filing_step1_scope.py --dry-run
    python scripts/fix_timely_filing_step1_scope.py --apply

    # local (override with env)
    PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres PG_PASSWORD=postgres \
    PG_DATABASE=uhc_backend python scripts/fix_timely_filing_step1_scope.py --apply
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

# ── Hardcoded PROD Postgres defaults (env vars still override via setdefault) ──
HARDCODED_PG = {
    "PG_HOST": "azure-pgsql-flexibleserver-np-390744103630-dev.privatelink.postgres.database.azure.com",
    "PG_PORT": "5432",
    "PG_USER": "pgazdev",
    "PG_PASSWORD": "Xudzab-doxsoz-1vudra",
    "PG_DATABASE": "uhc_backend",
}

STEP_NUMBER = 1
NEW_ACTION = "Proceed to the next step to verify timely filing."


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Un-stop Timely Filing Step 1 so it proceeds to verify."
    )
    ap.add_argument(
        "--title-like",
        default="timely",
        help="ILIKE token matching AuditSop.title (default: timely).",
    )
    ap.add_argument(
        "--workflow",
        default="",
        help="Restrict NodeRuleBinding action rewrite to this "
        "workflow id (default: all workflows using the SOP).",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Preview only; write nothing."
    )
    ap.add_argument("--apply", action="store_true", help="Commit the changes.")
    opts = ap.parse_args()
    dry = not opts.apply

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sop_backend.settings")
    for key, val in HARDCODED_PG.items():
        os.environ.setdefault(key, val)

    import django

    django.setup()

    from django.conf import settings
    from django.db import transaction

    from django.db.models import Q

    from agent_tools.models import NodeRuleBinding
    from sop_ingestion.models import AuditDecision, AuditSop, AuditStep

    db = settings.DATABASES["default"]
    print("── Target DB ───────────────────────────────────────────────")
    print(f"  HOST = {db.get('HOST')}  PORT = {db.get('PORT')}")
    print(f"  NAME = {db.get('NAME')}  USER = {db.get('USER')}")
    print(f"  mode = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    print(f"  workflow filter = {opts.workflow or '(all)'}")
    print("────────────────────────────────────────────────────────────")

    tf_sop_ids = list(
        AuditSop.objects.filter(title__icontains=opts.title_like).values_list(
            "id", flat=True
        )
    )
    if not tf_sop_ids:
        sys.exit(f"No AuditSop matched title~{opts.title_like!r}.")
    print(f"Timely Filing SOP id(s): {sorted(tf_sop_ids)}\n")

    n_dec = 0
    n_bind = 0

    def _do() -> None:
        nonlocal n_dec, n_bind
        for sop_id in sorted(tf_sop_ids):
            step = AuditStep.objects.filter(
                sop_id=sop_id, step_number=STEP_NUMBER
            ).first()
            if step is None:
                print(f"  [SKIP] sop_id={sop_id}: no Step {STEP_NUMBER}.")
                continue

            # The terminating branch(es): any Step 1 decision row flagged
            # out-of-scope or final. In Timely Filing this is the "No" /
            # "This P&P does not apply." row only.
            rows = list(
                AuditDecision.objects.filter(step=step)
                .filter(Q(is_out_of_scope=True) | Q(is_final=True))
                .order_by("row_index")
            )
            if not rows:
                print(
                    f"  SOP {sop_id}: Step {STEP_NUMBER} already in scope "
                    f"(no OOS/final rows) — nothing to fix."
                )
                continue

            print(f"  SOP {sop_id}  step_id={step.id}  " f"Q={step.question[:50]!r}")
            for d in rows:
                print(
                    f"      row{d.row_index} [{d.subrule_id}] "
                    f"oos {d.is_out_of_scope}->False  final {d.is_final}->False"
                    f"  IF={d.condition_if!r}"
                )
                d.is_out_of_scope = False
                d.is_final = False
                # Rewrite the terminal "does not apply" action to a proceed.
                if "does not apply" in (d.action_text or "").lower():
                    d.action_text = NEW_ACTION
                    d.action_summary = NEW_ACTION
                if not dry:
                    d.save(
                        update_fields=[
                            "is_out_of_scope",
                            "is_final",
                            "action_text",
                            "action_summary",
                        ]
                    )
                n_dec += 1

                # Rewrite the matching binding action override so the engine's
                # prompt + the dashboard reflect "proceed", not "does not apply".
                rule_key = f"step:{sop_id}:{STEP_NUMBER}:{d.row_index}"
                bq = NodeRuleBinding.objects.filter(sop_id=sop_id, rule_key=rule_key)
                if opts.workflow:
                    bq = bq.filter(
                        shape__workbench__work_area__workflow_id=opts.workflow
                    )
                for b in bq:
                    if "does not apply" not in (b.action or "").lower():
                        continue
                    print(
                        f"        binding {str(b.shape_id)[:8]} "
                        f"action -> {NEW_ACTION!r}"
                    )
                    b.action = NEW_ACTION
                    if not dry:
                        b.save(update_fields=["action"])
                    n_bind += 1

    if dry:
        _do()
        print(
            f"\nDRY-RUN: would clear OOS/final on {n_dec} Step {STEP_NUMBER} "
            f"row(s) and rewrite {n_bind} binding action(s). "
            f"Re-run with --apply to commit."
        )
    else:
        with transaction.atomic():
            _do()
        print(
            f"\nAPPLIED: cleared OOS/final on {n_dec} Step {STEP_NUMBER} "
            f"row(s); rewrote {n_bind} binding action(s)."
        )


if __name__ == "__main__":
    main()
