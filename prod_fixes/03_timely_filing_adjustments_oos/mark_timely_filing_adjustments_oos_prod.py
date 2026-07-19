#!/usr/bin/env python3
r"""Mark the Timely-Filing "Adjustments/Appeals Submission" rule OUT OF SCOPE.

OFFLINE / DETERMINISTIC — NO API CALLS, NO MCP, NO LLM, NO TOOL CALLS, NO RE-RUN.
Everything is done straight in the Postgres the UI reads.

The auditor ticket (e.g. claim 25XJ83640500)
---------------------------------------------
On the Timely-Filing SOP (Process 6 · Timely Filing · Step 5) the agent marked
the row

    step:7:5:5   RULE-005-006   (BYPASS)
    IF: Adjustments/Appeals Submission AND
        • INN providers - 365 days from date claim was processed/paid.
        • OON providers - 90 days …

as **matched**. Auditors flagged this as a bug: Adjustments/Appeals submissions
have their own timely-filing windows and are handled by a SEPARATE process — they
are NOT audited by this claim's timely-filing check. The row is therefore OUT OF
SCOPE, not a match. The other Step-5 submission branches (COB, Resubmission/
Corrected, New-day claim) remain IN SCOPE and are untouched.

Why "out of scope" and not the ``AuditDecision.is_out_of_scope`` flag
---------------------------------------------------------------------
``AuditDecision.is_out_of_scope`` is a ROUTING/HALT flag ("when Met, stop auditing
this path") — the wrong tool here. The engine's clean, non-halting exclusion is
``Shape.properties['manual_oos_rule_keys']`` (rule_loader honours it → the rule is
skipped with no LLM, independent of routing). This script uses that.

Two phases, both no-LLM / no-tool
---------------------------------
Phase A — WORKFLOW DEFINITION (once)
    1. Adds ``step:7:5:5`` to the Timely-Filing Step-5 Shape's
       ``properties['manual_oos_rule_keys']`` so FUTURE runs skip it as out of
       scope (no LLM, no halt, no routing side effect).
    2. Appends a guidance clause to the Workbench ``extra_context``.

Phase B — BACKFILL EXISTING RUNS (per claim)
    1. ``RuleEvaluation`` step:7:5:5 -> skipped=True with an ``out of scope:``
       skip_reason, matched=False, reasoning rewritten.
    2. ``ClaimTrace`` sub-rule RULE-005-006 -> status ``Skipped`` + an
       out-of-scope statement; stale "…matched/verified…" rationale segments
       removed; step status / final_status / explainability recomputed.
    3. ``ClaimExecutiveSummary`` — a stale Step-5 "matched/verified" note (if
       present) is softened to an out-of-scope-aware note. No regen, no LLM.

The row is ``decision_type=BYPASS`` (non-adverse), so dropping it from the rollup
is a NON-FINDING: the claim verdict (ALLOW/DENY/PEND) is UNCHANGED and a clean
claim stays clean — this only changes how the step is presented.

Idempotent: a row already out of scope is left untouched. ``--dry-run`` (default)
previews; ``--apply`` writes.

Usage (prod box — baked-in prod PG, no network needed):
    python scripts/mark_timely_filing_adjustments_oos_prod.py --dry-run
    python scripts/mark_timely_filing_adjustments_oos_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend \
    python scripts/mark_timely_filing_adjustments_oos_prod.py --apply
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
    "NO_LLM": "1",
    "PG_HOST": "azure-pgsql-flexibleserver-np-390744103630-dev.privatelink.postgres.database.azure.com",
    "PG_PORT": "5432",
    "PG_USER": "pgazdev",
    "PG_PASSWORD": "Xudzab-doxsoz-1vudra",
    "PG_DATABASE": "uhc_backend",
}

SOP_ID = 7
STEP_NUMBER = 5
ROW = 5
RULE_KEY = f"step:{SOP_ID}:{STEP_NUMBER}:{ROW}"
SUBRULE_ID = "RULE-005-006"
LABEL = "Adjustments/Appeals Submission"
WHY = (
    "adjustments and appeals submissions have their own timely-filing windows "
    "and are handled by a separate process — they are not audited by this "
    "claim's timely-filing check"
)

# Idempotency marker — unique opening of the out-of-scope reasoning.
_OOS_MARKER = "Out of scope for the Timely-Filing audit"

_REASONING = (
    f"{LABEL}: Out of scope for the Timely-Filing audit. This branch covers "
    f"adjustments/appeals submissions, which {WHY}, so it is not a match and not "
    f"a discrepancy. Marked out of scope per auditor review."
)
_SKIP_REASON = (
    f"out of scope: {LABEL} is handled by a separate adjustments/appeals process "
    f"and is not audited by this claim's timely-filing check; excluded per "
    f"auditor review."
)[:255]
_STATEMENT = (
    f"{LABEL}: Out of scope — adjustments/appeals submissions are handled by a "
    f"separate process; not matched and not a discrepancy."
)

# Soften a stale Step-5 exec-summary note that asserts the adjustments/appeals
# branch matched/was verified.
_ES_NOTE = (
    " The Adjustments/Appeals submission branch is out of scope for the "
    "timely-filing audit (handled by a separate process)."
)
_ES_MARKER = "Adjustments/Appeals submission branch is out of scope"

_WORKFLOW_CLAUSE = (
    "\n\nTIMELY-FILING OUT-OF-SCOPE BRANCH (auditor-confirmed): In Step 5, the "
    "'Adjustments/Appeals Submission' branch (INN 365 days / OON 90 days from the "
    "date the claim was processed/paid) is OUT OF SCOPE for this audit — "
    "adjustments and appeals have their own timely-filing windows and are handled "
    "by a separate process. Do NOT mark it matched/verified and do NOT report it "
    "as a discrepancy. The COB, Resubmission/Corrected-Claim and New-day-claim "
    "submission branches remain in scope."
)
_WORKFLOW_MARKER = "TIMELY-FILING OUT-OF-SCOPE BRANCH (auditor-confirmed)"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Mark the Timely-Filing Adjustments/Appeals branch out of "
                    "scope (offline, no API/MCP/LLM/tools).")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--claim", action="append", default=[],
                    help="Restrict backfill to these claim id(s) (repeatable).")
    ap.add_argument("--skip-workflow", action="store_true",
                    help="Skip Phase A (assume manual-OOS key already set).")
    ap.add_argument("--skip-backfill", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    opts = ap.parse_args()
    dry = not opts.apply

    for k, v in _PROD_ENV.items():
        os.environ.setdefault(k, v)
    os.environ["NO_LLM"] = "1"
    os.environ["LLM_BACKEND"] = "none"
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    import django
    django.setup()

    from django.db import transaction
    from agent_tools.models import NodeRuleBinding
    from execution_app import trace_builder
    from execution_app.models import (ClaimExecutiveSummary, ClaimTrace,
                                       RuleEvaluation, RuleExecutionRun)
    from execution_app.trace_builder import _build_explainability, _iso

    _p("── Timely Filing: mark Adjustments/Appeals branch OUT OF SCOPE [OFFLINE] ──")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")
    _p(f"  target      = {RULE_KEY}  ({SUBRULE_ID}  {LABEL})")
    _p("────────────────────────────────────────────────────────────")

    # ── Phase A — workflow definition (manual OOS key + guidance) ──
    tf_shape_id = None
    if not opts.skip_workflow:
        _p("══ Phase A — manual out-of-scope key + Workbench guidance ══")
        anchor = (
            NodeRuleBinding.objects.filter(
                sop_id=SOP_ID,
                shape__workbench__work_area__workflow_id=opts.workflow,
                rule_key=RULE_KEY,
            ).select_related("shape", "shape__workbench").first()
        )
        if anchor is None:
            _p(f"  WARNING: no {RULE_KEY} binding found — cannot set manual OOS "
               "key (backfill still applies).")
        else:
            shape = anchor.shape
            tf_shape_id = str(shape.id)
            props = shape.properties if isinstance(shape.properties, dict) else {}
            have = set(props.get("manual_oos_rule_keys") or [])
            if RULE_KEY not in have:
                _p(f"  [{'would set' if dry else 'set'}] shape {shape.id} "
                   f"manual_oos_rule_keys += ['{RULE_KEY}']")
                if not dry:
                    props["manual_oos_rule_keys"] = sorted(have | {RULE_KEY})
                    shape.properties = props
                    shape.save(update_fields=["properties"])
            else:
                _p(f"  [have] shape {shape.id} — {RULE_KEY} already manual-OOS")

            wb = shape.workbench
            cfg = wb.config if isinstance(wb.config, dict) else {}
            ec = cfg.get("extra_context") or ""
            if _WORKFLOW_MARKER in ec:
                _p(f"  [have] workbench {wb.id} — guidance already present")
            else:
                _p(f"  [{'would edit' if dry else 'edit'}] workbench {wb.id} "
                   f"extra_context (+{len(_WORKFLOW_CLAUSE)} chars)")
                if not dry:
                    cfg["extra_context"] = ec + _WORKFLOW_CLAUSE
                    wb.config = cfg
                    wb.save(update_fields=["config"])

    if opts.skip_backfill:
        _p("\n(skip-backfill) — workflow updated; existing claims untouched.")
        return

    # ── Phase B — backfill existing runs ──
    _p("\n══ Phase B — backfill existing claim runs ══")
    latest: dict[str, RuleExecutionRun] = {}
    q = RuleExecutionRun.objects.filter(workflow_id=opts.workflow)
    if opts.claim:
        q = q.filter(claim_id__in=opts.claim)
    for run in q.order_by("claim_id", "-started_at").only(
        "id", "claim_id", "started_at", "finished_at"
    ):
        if run.claim_id and run.claim_id not in latest:
            latest[run.claim_id] = run

    run_ids = [r.id for r in latest.values()]
    ev_by_run: dict = {
        e.run_id: e
        for e in RuleEvaluation.objects.filter(run_id__in=run_ids, rule_key=RULE_KEY)
    }

    def _fix_one(run) -> str:
        ev = ev_by_run.get(run.id)
        if ev is None:
            return "SKIP: no target evaluation on run"

        already = ev.skipped and _OOS_MARKER in (ev.reasoning or "")

        # 1) RuleEvaluation row -> out of scope (skipped, non-finding).
        if not already:
            if not dry:
                ev.matched = False
                ev.skipped = True
                ev.skip_reason = _SKIP_REASON
                ev.verdict = ""
                ev.reasoning = _REASONING
                ev.save(update_fields=["matched", "skipped", "skip_reason",
                                       "verdict", "reasoning"])

        # 2) ClaimTrace sub-rule -> Skipped + out-of-scope statement.
        ct = ClaimTrace.objects.filter(run=run).first()
        tchanged = False
        if ct and isinstance(ct.trace_json, list):
            for entry in ct.trace_json:
                if str(entry.get("sop_step_number")) != str(STEP_NUMBER):
                    continue
                if "timely" not in (entry.get("sop_name") or "").lower():
                    continue
                for sr in (entry.get("subrule_results") or []):
                    if str(sr.get("subrule_id")) == SUBRULE_ID:
                        if sr.get("status") != "Skipped" or sr.get("statement") != _STATEMENT:
                            sr["status"] = "Skipped"
                            sr["statement"] = _STATEMENT
                            sr["label"] = LABEL
                            tchanged = True
                # Drop stale "Adjustments/Appeals …matched/verified…" rationale.
                rat = entry.get("rationale")
                if isinstance(rat, str) and rat:
                    segs = [s.strip() for s in rat.split(";")]
                    keep = [s for s in segs
                            if not (("adjustment" in s.lower() or "appeal" in s.lower())
                                    and re.search(r"match|verif", s, re.I))]
                    new_rat = "; ".join([s for s in keep if s])
                    if new_rat != rat:
                        entry["rationale"] = new_rat
                        tchanged = True
                # Recompute step status from the surviving (non-skipped) sub-rules.
                srs = entry.get("subrule_results") or []
                live = [s for s in srs
                        if str(s.get("status") or "").lower()
                        not in ("skipped", "skip", "n/a", "na")]
                if live and all(str(s.get("status")) == "Met" for s in live):
                    entry["status"] = "Met"
                elif not live and entry.get("status") not in ("Skipped",):
                    entry["status"] = "Skipped"
            if tchanged and not dry:
                ct.final_status = trace_builder.claim_status(ct.trace_json)
                ct.explainability_json = _build_explainability(
                    ct.trace_json, str(run.id), run.claim_id,
                    _iso(run.started_at), _iso(run.finished_at), run)
                ct.save(update_fields=["trace_json", "explainability_json",
                                       "final_status", "updated_at"])

        # 3) Executive summary — soften a stale Step-5 "matched" note.
        if not dry:
            es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
            if es is not None and isinstance(es.step_summaries, list):
                steps = list(es.step_summaries)
                eschanged = False
                for st in steps:
                    if not isinstance(st, dict):
                        continue
                    sid = str(st.get("shape_id") or "")
                    summ = str(st.get("summary") or "")
                    hit_shape = tf_shape_id and sid == tf_shape_id
                    hit_text = ("adjustment" in summ.lower() or "appeal" in summ.lower())
                    if not (hit_shape or hit_text):
                        continue
                    if _ES_MARKER in summ:
                        continue
                    st["summary"] = (summ.rstrip() + _ES_NOTE).strip()
                    eschanged = True
                if eschanged:
                    es.step_summaries = steps
                    es.save(update_fields=["step_summaries", "updated_at"])

        if already and not tchanged:
            return "already OOS"
        return "Adjustments/Appeals -> OUT OF SCOPE"

    changed = already = missing = failed = 0
    claim_ids = sorted(latest)
    total = len(claim_ids)
    for i, cid in enumerate(claim_ids, 1):
        run = latest[cid]
        try:
            if dry:
                note = _fix_one(run)
            else:
                with transaction.atomic():
                    note = _fix_one(run)
        except Exception as exc:  # pragma: no cover - defensive
            failed += 1
            _p(f"[{i}/{total}] {cid} run={run.id} FAILED: {exc}")
            continue
        if note == "already OOS":
            already += 1
        elif note.startswith("SKIP"):
            missing += 1
        else:
            changed += 1
            _p(f"[{i}/{total}] {cid} run={run.id} [CHANGED] {note}")
        if i % 25 == 0 or i == total:
            _p(f"PROGRESS {i}/{total}  changed={changed} already_oos={already} "
               f"missing={missing} failed={failed}")

    _p("────────────────────────────────────────────────────────────")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  changed to OOS = {changed}")
    _p(f"  already OOS    = {already}")
    _p(f"  no target eval = {missing}")
    _p(f"  failed         = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
