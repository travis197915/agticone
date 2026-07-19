#!/usr/bin/env python3
r"""Mark the non-image Physician Claim Checklist fields OUT OF SCOPE (SOP 14).

OFFLINE / DETERMINISTIC — NO API CALLS, NO MCP, NO LLM, NO TOOL CALLS, NO RE-RUN.
Everything is done straight in the Postgres the UI reads.

The auditor ticket (e.g. claim 25XJ88210600)
---------------------------------------------
On the Physician Claim Checklist (Process 1 · Initial Verification · Step 1) the
agent marked a set of mandatory fields as "matched / Verified per SOP" even though
they are NOT present on the Doc360 claim image or the Facets indicative screen for
a physician (professional / CMS-1500) claim. Auditors flagged these as bugs — the
fields are OUT OF SCOPE for this checklist, not a clean match:

    COB (Coordination of Benefits) ........ Rule #227
    Authorization ......................... Rule #229   (no auth data on image)
    Type of Service ....................... (PDF: highlighted out of scope)
    Statement Covers Period (To & From) ... Rule #229   (UB-04 institutional field)
    Type of Bill .......................... Rule #232   (UB-04 institutional field)
    Accommodation Rates ................... Rule #233   (UB-04 institutional field)
    Copay ................................. Rule #234   (adjudication output)
    Coinsurance ........................... Rule #235   (adjudication output)
    Deductible ............................ Rule #236   (adjudication output)
    Paid Amount ........................... (PDF: highlighted out of scope)

These are exactly the fields added by ``add_physician_checklist_fields_prod.py``
(rows 10..20). ``Provider ID (TIN)`` (row 11) was NOT flagged and is left in scope.

Why "out of scope" and not the ``AuditDecision.is_out_of_scope`` flag
---------------------------------------------------------------------
``AuditDecision.is_out_of_scope`` is a ROUTING/HALT flag ("when Met, stop auditing
this path") — the wrong tool here. The engine's clean, non-halting exclusion is
``Shape.properties['manual_oos_rule_keys']`` (rule_loader honours it → the rule is
skipped with no LLM, independent of routing). This script uses that.

Two phases, both no-LLM / no-tool
---------------------------------
Phase A — WORKFLOW DEFINITION (once)
    1. Adds the 10 rule_keys to the Physician-Checklist Step-1 Shape's
       ``properties['manual_oos_rule_keys']`` so FUTURE runs skip them as
       out of scope (no LLM, no halt).
    2. Appends a guidance clause to the Workbench ``extra_context``.

Phase B — BACKFILL EXISTING RUNS (per claim)
    1. ``RuleEvaluation`` step:14:1:{10,12..20} -> skipped=True with an
       ``out of scope:`` skip_reason, matched=False, reasoning rewritten.
    2. ``ClaimTrace`` sub-rules RULE-001-011/013..021 -> status ``Skipped`` +
       an out-of-scope statement; stale "…verified…" rationale segments removed;
       step status / final_status / explainability recomputed (stays CLEAN — an
       out-of-scope row is a NON-FINDING dropped from the rollup).
    3. ``ClaimExecutiveSummary`` — the stale "all mandatory fields verified" note
       (if a prior script added it) is softened to an out-of-scope-aware note.
       No regeneration, no LLM.

These rules are ``decision_type=CONDITIONAL`` (non-adverse), so the run verdict
(ALLOW/DENY/PEND) is UNCHANGED — this only changes how the checklist is presented.

Idempotent: a row already out of scope is left untouched. ``--dry-run`` (default)
previews; ``--apply`` writes.

Usage (prod box — baked-in prod PG, no network needed):
    python scripts/mark_physician_checklist_oos_prod.py --dry-run
    python scripts/mark_physician_checklist_oos_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend \
    python scripts/mark_physician_checklist_oos_prod.py --apply
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

SOP_ID = 14
STEP_NUMBER = 1

# ── The out-of-scope field set: (row_index, subrule_id, label, why) ──
# Union of the auditor bug rows and the PDF yellow highlights. Provider ID (TIN)
# (row 11) is deliberately NOT included (not flagged / not highlighted).
FIELDS: tuple[tuple[int, str, str, str], ...] = (
    (10, "RULE-001-011", "COB (Coordination of Benefits)",
     "Coordination-of-Benefits is an adjudication detail, not carried on the claim image"),
    (12, "RULE-001-013", "Authorization",
     "no authorization data is present on the claim image or the indicative screen"),
    (13, "RULE-001-014", "Type of Service",
     "not present on the claim image or the indicative screen"),
    (14, "RULE-001-015", "Statement Covers Period (To & From)",
     "a UB-04 institutional field, not present on a physician (professional) claim image"),
    (15, "RULE-001-016", "Type of Bill",
     "a UB-04 institutional field, not present on a physician (professional) claim image"),
    (16, "RULE-001-017", "Accommodation Rates",
     "a UB-04 institutional field, not present on a physician (professional) claim image"),
    (17, "RULE-001-018", "Copay",
     "an adjudication output, not carried on the claim image or the indicative screen"),
    (18, "RULE-001-019", "Coinsurance",
     "an adjudication output, not carried on the claim image or the indicative screen"),
    (19, "RULE-001-020", "Deductible",
     "an adjudication output, not carried on the claim image or the indicative screen"),
    (20, "RULE-001-021", "Paid Amount",
     "an adjudication output, not carried on the claim image or the indicative screen"),
)

_ROWS = {f"step:{SOP_ID}:{STEP_NUMBER}:{row}" for row, *_ in FIELDS}
_SUBRULE_IDS = {sid for _, sid, *_ in FIELDS}
_BY_SUBRULE = {sid: (row, sid, label, why) for row, sid, label, why in FIELDS}

# Idempotency marker — unique opening of the out-of-scope reasoning.
_OOS_MARKER = "Out of scope for the Physician Claim Checklist"

# Soften the stale "all mandatory fields verified" note a prior script may have
# appended to the exec-summary step line.
_ES_OLD_MARKER = "All mandatory physician-claim checklist fields were verified"
_ES_NEW_NOTE = (
    " The applicable physician-claim checklist fields were verified per SOP; "
    "fields not present on the claim image (Authorization, Type of Bill, "
    "Statement Covers Period, Copay, Coinsurance, Deductible, Accommodation "
    "Rates, Paid Amount, COB, Type of Service) are out of scope.")
_ES_NEW_MARKER = "are out of scope"

_WORKFLOW_CLAUSE = (
    "\n\nPHYSICIAN-CHECKLIST OUT-OF-SCOPE FIELDS (auditor-confirmed): On a "
    "physician (professional / CMS-1500) claim the following checklist fields are "
    "NOT present on the Doc360 claim image or the Facets indicative screen and are "
    "OUT OF SCOPE — do NOT mark them matched/verified and do NOT report them as a "
    "discrepancy: COB, Authorization, Type of Service, Statement Covers Period, "
    "Type of Bill, Accommodation Rates, Copay, Coinsurance, Deductible, Paid "
    "Amount. Provider ID (TIN vs auth), Member Name, Subscriber ID and the other "
    "image-verifiable fields remain in scope.")
_WORKFLOW_MARKER = "PHYSICIAN-CHECKLIST OUT-OF-SCOPE FIELDS (auditor-confirmed)"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _reasoning(label: str, why: str) -> str:
    return (
        f"{label}: Out of scope for the Physician Claim Checklist. This field is "
        f"{why}, so it cannot be verified against the claim image and is not a "
        f"discrepancy. Marked out of scope per auditor review."
    )


def _skip_reason(label: str) -> str:
    return (f"out of scope: {label} is not present on the Doc360 claim image / "
            f"indicative screen for a physician claim; excluded from the checklist "
            f"per auditor review.")


def _statement(label: str) -> str:
    return (f"{label}: Out of scope — not present on the Doc360 claim image / "
            f"indicative screen; not verified and not a discrepancy.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Mark the non-image Physician-Checklist fields out of scope "
                    "(offline, no API/MCP/LLM/tools).")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--claim", action="append", default=[],
                    help="Restrict backfill to these claim id(s) (repeatable).")
    ap.add_argument("--skip-workflow", action="store_true",
                    help="Skip Phase A (assume manual-OOS keys already set).")
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
    from builder.models import Workbench
    from execution_app import trace_builder
    from execution_app.models import (ClaimExecutiveSummary, ClaimTrace,
                                       RuleEvaluation, RuleExecutionRun)
    from execution_app.trace_builder import _build_explainability, _iso

    _p("── Physician Claim Checklist: mark non-image fields OUT OF SCOPE [OFFLINE] ──")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")
    _p(f"  fields      = {len(FIELDS)} -> {sorted(_ROWS)}")
    _p("────────────────────────────────────────────────────────────")

    # ── Phase A — workflow definition (manual OOS keys + guidance) ──
    if not opts.skip_workflow:
        _p("══ Phase A — manual out-of-scope keys + Workbench guidance ══")
        anchor = (
            NodeRuleBinding.objects.filter(
                sop_id=SOP_ID,
                shape__workbench__work_area__workflow_id=opts.workflow,
                rule_key__startswith=f"step:{SOP_ID}:{STEP_NUMBER}:",
            ).select_related("shape", "shape__workbench").order_by("ordering").first()
        )
        if anchor is None:
            _p("  WARNING: no step:14:1:* binding found — cannot set manual OOS "
               "keys (backfill still applies).")
        else:
            shape = anchor.shape
            props = shape.properties if isinstance(shape.properties, dict) else {}
            have = set(props.get("manual_oos_rule_keys") or [])
            want = have | _ROWS
            if want != have:
                _p(f"  [{'would set' if dry else 'set'}] shape {shape.id} "
                   f"manual_oos_rule_keys += {sorted(_ROWS - have)}")
                if not dry:
                    props["manual_oos_rule_keys"] = sorted(want)
                    shape.properties = props
                    shape.save(update_fields=["properties"])
            else:
                _p(f"  [have] shape {shape.id} — all 10 keys already manual-OOS")

            wb = anchor.shape.workbench
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
    evs_by_run: dict = {}
    for e in RuleEvaluation.objects.filter(run_id__in=run_ids, rule_key__in=_ROWS):
        evs_by_run.setdefault(e.run_id, {})[e.rule_key] = e

    def _fix_one(run) -> str:
        evs = evs_by_run.get(run.id, {})
        if not evs:
            return "SKIP: no target evaluations on run"

        # 1) RuleEvaluation rows -> out of scope (skipped, non-finding).
        flipped = 0
        for row, sid, label, why in FIELDS:
            ev = evs.get(f"step:{SOP_ID}:{STEP_NUMBER}:{row}")
            if ev is None:
                continue
            if ev.skipped and _OOS_MARKER in (ev.reasoning or ""):
                continue  # already OOS
            if not dry:
                ev.matched = False
                ev.skipped = True
                ev.skip_reason = _skip_reason(label)
                ev.verdict = ""
                ev.reasoning = _reasoning(label, why)
                ev.save(update_fields=["matched", "skipped", "skip_reason",
                                       "verdict", "reasoning"])
            flipped += 1
        if flipped == 0:
            return "already OOS"

        # 2) ClaimTrace sub-rules -> Skipped + out-of-scope statement.
        ct = ClaimTrace.objects.filter(run=run).first()
        if ct and isinstance(ct.trace_json, list):
            tchanged = False
            for entry in ct.trace_json:
                if str(entry.get("sop_step_number")) != str(STEP_NUMBER):
                    continue
                if "hysician" not in (entry.get("sop_name") or "").lower():
                    continue
                for sr in (entry.get("subrule_results") or []):
                    sid = str(sr.get("subrule_id"))
                    if sid in _SUBRULE_IDS:
                        _, _, label, _why = _BY_SUBRULE[sid]
                        sr["status"] = "Skipped"
                        sr["statement"] = _statement(label)
                        sr["label"] = label
                        tchanged = True
                # Drop stale "<label> verification: …verified…" rationale segments.
                rat = entry.get("rationale")
                if isinstance(rat, str) and rat:
                    segs = [s.strip() for s in rat.split(";")]
                    keep = [s for s in segs if not any(
                        s.startswith(f"{lab} verification")
                        for _r, _s, lab, _w in FIELDS)]
                    new_rat = "; ".join([s for s in keep if s])
                    if new_rat != rat:
                        entry["rationale"] = new_rat
                        tchanged = True
                # Recompute step status from the non-skipped sub-rules.
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

        # 3) Executive summary — soften the stale "all fields verified" note.
        if not dry:
            es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
            if es is not None and isinstance(es.step_summaries, list):
                steps = list(es.step_summaries)
                eschanged = False
                for st in steps:
                    if not isinstance(st, dict):
                        continue
                    summ = str(st.get("summary") or "")
                    if _ES_NEW_MARKER in summ:
                        continue
                    if _ES_OLD_MARKER in summ:
                        summ = re.sub(
                            r"\s*All mandatory physician-claim checklist fields "
                            r"were verified per SOP with no discrepancy\.",
                            "", summ).rstrip()
                        st["summary"] = (summ + _ES_NEW_NOTE).strip()
                        eschanged = True
                if eschanged:
                    es.step_summaries = steps
                    es.save(update_fields=["step_summaries", "updated_at"])

        return f"{flipped} field(s) -> OUT OF SCOPE"

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
