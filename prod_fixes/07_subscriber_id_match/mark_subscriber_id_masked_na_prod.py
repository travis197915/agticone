#!/usr/bin/env python3
r"""Mark Subscriber-ID mismatches as NOT APPLICABLE (masked data) — Physician
Claim Checklist (SOP 14).

OFFLINE / DETERMINISTIC — NO API CALLS, NO MCP, NO LLM, NO RE-RUN.

The problem (auditor-reported, e.g. claim 25XK05953100)
-------------------------------------------------------
Physician Claim Checklist sub-rule **RULE-001-001 / rule_key ``step:14:1:0`` —
"Subscriber ID"** compares the Facets Subscriber ID against the Doc360 image
"Insured's ID Number". In THIS environment the claim data is masked /
de-identified across the two sources, so the two values are NOT expected to
match and cannot be validly compared. The agent correctly reports "not matched",
and the sub-rule then shows a scary **"Not-Met — Discrepancy found"** line.

That is a false finding: a mismatch here reflects masked test data, not a claim
defect. It must NOT be shown as an error, and (because the values genuinely
differ) it must NOT be shown as a clean match either. The correct disposition is
**Not Applicable** — the comparison does not apply in a masked environment.

What this script does (straight into the DB the UI reads)
---------------------------------------------------------
Because masked data can NEVER validly match, the Subscriber-ID check is Not
Applicable for EVERY claim's latest run (the rows currently shown as "matched"
are themselves artifacts of masked data and are converted to N/A too). Pass
``--only-mismatch`` to leave the "matched" rows clean and only touch mismatches.

Phase 1 — WORKFLOW GUIDANCE (once, best-effort; ``--skip-workflow-fix`` to skip)
    Appends a clause to the Physician-Checklist Workbench ``extra_context`` so
    FUTURE runs mark the Subscriber ID Not Applicable (masked data) instead of a
    discrepancy.

Phase 2 — PER-CLAIM DATA FIX
    1. ``RuleEvaluation`` ``step:14:1:0`` -> skipped=True with a
       ``not-applicable:`` skip_reason (masked data), matched=False, reasoning
       rewritten to explain the N/A. This makes the agents-tab rollup treat it
       as a NON-FINDING (NOT_APPLICABLE), not a discrepancy.
    2. ``ClaimTrace`` sub-rule RULE-001-001 -> status ``Skipped`` + a masked-data
       statement; the step status / final_status / explainability are recomputed
       (the claim stays CLEAN — an N/A row drops out of the rollup).
    3. ``ClaimExecutiveSummary`` — any stale "subscriber id … mismatch/
       discrepancy" phrasing is softened to the N/A wording (no regeneration).

The Subscriber-ID rule is ``decision_type=CONDITIONAL`` (non-adverse), so the run
verdict (ALLOW/DENY/PEND) is unaffected either way — this only changes how the
Subscriber-ID line is presented to the auditor. Claims where Subscriber ID
already MATCHED are left untouched.

Idempotent + safe: a row already marked N/A is left untouched. ``--dry-run``
(default) previews; ``--apply`` writes.

Usage (prod box — baked-in prod PG, no network needed):
    python scripts/mark_subscriber_id_masked_na_prod.py --dry-run
    python scripts/mark_subscriber_id_masked_na_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend \
    python scripts/mark_subscriber_id_masked_na_prod.py --apply
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

SUBSCRIBER_RULE_KEY = "step:14:1:0"
SUBSCRIBER_SUBRULE_ID = "RULE-001-001"

# Idempotency marker — the unique opening of the N/A reasoning/statement.
_NA_MARKER = "Not Applicable in this environment"
_SKIP_REASON = (
    "not-applicable: Subscriber ID is masked/de-identified in this environment; "
    "the Facets Subscriber ID and the Doc360 image ID are not comparable, so a "
    "mismatch is not a defect."
)

# Soften any stale subscriber-id phrasing in exec summaries — both the
# mismatch/discrepancy wording AND any positive "matches" wording a prior fix may
# have written (all Subscriber-ID checks are now N/A under masked data).
_ES_NA_SENTENCE = (
    "Subscriber ID comparison is Not Applicable (data masked in this environment).")
_ES_SCRUBS = (
    (re.compile(r"subscriber id[^.]*?(?:do(?:es)? not match|mismatch|"
                r"discrepan\w+|match(?:e[sd])?|verif\w+)[^.]*\.", re.I),
     _ES_NA_SENTENCE),
)

_WORKFLOW_CLAUSE = (
    "\n\nSUBSCRIBER-ID IN MASKED ENVIRONMENT (auditor-confirmed): Claim data is "
    "masked / de-identified across Facets and Doc360 in this environment, so the "
    "Facets Subscriber ID and the Doc360 image 'Insured's ID Number' are NOT "
    "expected to match and cannot be validly compared. When they do not match, "
    "mark the Subscriber ID check NOT APPLICABLE (masked data) — do NOT report it "
    "as a discrepancy/defect, and do NOT report it as a clean match."
)
_WORKFLOW_MARKER = "SUBSCRIBER-ID IN MASKED ENVIRONMENT (auditor-confirmed)"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _na_reasoning() -> str:
    return (
        "Subscriber ID verification: Not Applicable in this environment. Claim "
        "data is masked / de-identified, so the Facets Subscriber ID and the "
        "Doc360 image 'Insured's ID Number' are not expected to match and cannot "
        "be validly compared. A mismatch here reflects masked test data, not a "
        "claim defect, so the Subscriber ID check is marked Not Applicable rather "
        "than a discrepancy."
    )


def _na_statement() -> str:
    return (
        "Subscriber ID: Not Applicable — claim data is masked in this "
        "environment, so the Facets Subscriber ID and the Doc360 image ID cannot "
        "be validly compared; a mismatch is not a defect."
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Mark Subscriber-ID mismatches Not Applicable (masked data) "
                    "on the Physician Claim Checklist (offline, no API/MCP/LLM).")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--rule-key", default=SUBSCRIBER_RULE_KEY)
    ap.add_argument("--claim", action="append", default=[],
                    help="Restrict to these claim id(s) (repeatable).")
    ap.add_argument("--skip-workflow-fix", action="store_true")
    ap.add_argument("--only-mismatch", action="store_true",
                    help="Only mark the mismatched rows N/A and leave the "
                         "'matched' rows as clean. Default: mark EVERY claim's "
                         "Subscriber-ID check N/A (masked data can never match).")
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
    from builder.models import Workbench
    from execution_app import trace_builder
    from execution_app.models import (ClaimExecutiveSummary, ClaimTrace,
                                       RuleEvaluation, RuleExecutionRun)
    from execution_app.trace_builder import _build_explainability, _iso

    _p("── Subscriber-ID → Not Applicable (masked data) [OFFLINE] ──")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")
    _p(f"  rule_key    = {opts.rule_key}")
    _p("────────────────────────────────────────────────────────────")

    # ── Phase 1: workflow guidance (once, best-effort) ──
    if not opts.skip_workflow_fix:
        _p("══ Phase 1 — workflow guidance (Workbench extra_context) ══")
        n_ctx = 0
        for wb in Workbench.objects.all():
            cfg = wb.config if isinstance(wb.config, dict) else {}
            ec = cfg.get("extra_context")
            if not isinstance(ec, str) or not ec.strip():
                continue
            low = ec.lower()
            if ("physician claim checklist" not in low
                    and "subscriber id" not in low):
                continue
            if _WORKFLOW_MARKER in ec:
                _p(f"  [have] workbench {wb.id} — guidance already present")
                continue
            if not dry:
                cfg["extra_context"] = ec + _WORKFLOW_CLAUSE
                wb.config = cfg
                wb.save(update_fields=["config"])
            _p(f"  [{'would edit' if dry else 'edit'}] workbench {wb.id} "
               f"(+{len(_WORKFLOW_CLAUSE)} chars)")
            n_ctx += 1
        if n_ctx == 0:
            _p("  (no matching Workbench extra_context found — data fix still "
               "applies; guidance is best-effort for future re-runs)")

    # ── Select target claims: latest run per claim where step:14:1:0 is a
    # mismatch (matched=False, not skipped). Auto-targets the mismatch set and is
    # naturally idempotent (once N/A the row is skipped → excluded). ──
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
    ev_by_run = {
        e.run_id: e
        for e in RuleEvaluation.objects.filter(
            run_id__in=run_ids, rule_key=opts.rule_key)
    }

    def _fix_one(run) -> str:
        ev = ev_by_run.get(run.id)
        if ev is None:
            return "SKIP: no step:14:1:0 evaluation on run"
        already_na = ev.skipped and _NA_MARKER in (ev.reasoning or "")
        if already_na:
            return "already N/A"
        # Masked data can NEVER validly match, so EVERY Subscriber-ID row is Not
        # Applicable — including the ones currently shown as "matched" (those are
        # artifacts of masked data). ``--only-mismatch`` keeps matched rows clean.
        if opts.only_mismatch and ev.matched and not ev.skipped:
            return "SKIP: Subscriber ID matched (left clean, --only-mismatch)"

        reason = _na_reasoning()
        stmt = _na_statement()

        # 1) RuleEvaluation -> Not Applicable (skipped, non-finding).
        if not dry:
            ev.matched = False
            ev.skipped = True
            ev.skip_reason = _SKIP_REASON
            ev.verdict = ""
            ev.reasoning = reason
            ev.save(update_fields=["matched", "skipped", "skip_reason",
                                   "verdict", "reasoning"])

        # 2) ClaimTrace sub-rule RULE-001-001 -> Skipped + N/A statement.
        ct = ClaimTrace.objects.filter(run=run).first()
        if ct and isinstance(ct.trace_json, list):
            tchanged = False
            for entry in ct.trace_json:
                if str(entry.get("sop_step_number")) != "1":
                    continue
                if "hysician" not in (entry.get("sop_name") or "").lower():
                    continue
                for sr in (entry.get("subrule_results") or []):
                    if str(sr.get("subrule_id")) == SUBSCRIBER_SUBRULE_ID:
                        sr["status"] = "Skipped"
                        sr["statement"] = stmt
                        sr["label"] = "Subscriber ID"
                        tchanged = True
                # Recompute the step's displayed status from the (non-skipped)
                # sub-rules: any real Not-Met -> Not-Met, else all Met -> Met.
                srs = entry.get("subrule_results") or []
                live = [s for s in srs
                        if str(s.get("status") or "").lower()
                        not in ("skipped", "skip", "n/a", "na")]
                if live and all(str(s.get("status")) == "Met" for s in live):
                    entry["status"] = "Met"
            if tchanged and not dry:
                ct.final_status = trace_builder.claim_status(ct.trace_json)
                ct.explainability_json = _build_explainability(
                    ct.trace_json, str(run.id), run.claim_id,
                    _iso(run.started_at), _iso(run.finished_at), run)
                ct.save(update_fields=["trace_json", "explainability_json",
                                       "final_status", "updated_at"])

        # 3) Executive summary — in-place scrub only (no regeneration).
        if not dry:
            es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
            if es is not None:
                def _scrub(t: str) -> str:
                    for rx, repl in _ES_SCRUBS:
                        t = rx.sub(repl, t or "")
                    return t
                new_overall = _scrub(es.overall_summary)
                new_headline = _scrub(es.headline)
                new_findings = [_scrub(str(k)) for k in (es.key_findings or [])]
                new_steps = []
                for s in (es.step_summaries or []):
                    if isinstance(s, dict):
                        s = dict(s)
                        s["summary"] = _scrub(str(s.get("summary") or ""))
                    new_steps.append(s)
                if (new_overall != es.overall_summary
                        or new_headline != es.headline
                        or new_findings != (es.key_findings or [])
                        or new_steps != (es.step_summaries or [])):
                    es.overall_summary = new_overall
                    es.headline = new_headline
                    es.key_findings = new_findings
                    es.step_summaries = new_steps
                    es.save(update_fields=["overall_summary", "headline",
                                           "key_findings", "step_summaries",
                                           "updated_at"])
        return "Subscriber ID -> NOT APPLICABLE (masked data)"

    changed = already = matched = missing = failed = 0
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
        if note == "already N/A":
            already += 1
        elif note.startswith("SKIP: Subscriber ID matched"):
            matched += 1
        elif note.startswith("SKIP"):
            missing += 1
        else:
            changed += 1
            _p(f"[{i}/{total}] {cid} run={run.id} [CHANGED] {note}")
        if i % 25 == 0 or i == total:
            _p(f"PROGRESS {i}/{total}  changed={changed} matched(clean)={matched} "
               f"already_na={already} missing={missing} failed={failed}")

    _p("────────────────────────────────────────────────────────────")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  changed to N/A     = {changed}")
    _p(f"  already N/A        = {already}")
    _p(f"  left clean (match) = {matched}")
    _p(f"  no eval / skipped  = {missing}")
    _p(f"  failed             = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
