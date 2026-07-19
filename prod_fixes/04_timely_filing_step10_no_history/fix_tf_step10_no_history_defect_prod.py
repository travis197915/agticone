#!/usr/bin/env python3
r"""Clear the FALSE Timely-Filing "Step 10 (No Claim in History)" defect —
deterministically, NO LLM.

Auditor report (UAT prod)
-------------------------
    25XJ83640500  ·  Process 6 Timely Filing · Step 10 reflects defect.
    "Agent did determine the claim was denied for timely filing correctly and
     should be clean. There is no claim in history."

Root cause (verified against the persisted rows)
------------------------------------------------
On a NEW-DAY claim that was correctly denied for timely filing and has NO
matching claim in history, the Timely-Filing SOP evaluates two step-10 branches
that are NOT audit findings, yet the stored run is flipped to DENY / DEFECT:

  (A) TRACE side — the step-10 routing branch
        rule:   step:7:10:2  "No claim/line in history, Freq 7/8 Submission"
        action: "Follow OBH Facets Kill-Delete Reroute Process …"
      The branch simply does not apply (no history / not a Freq 7-8 resubmit),
      so the trace entry is rendered **Not-Met (CONDITIONAL)**. But
      ``trace_builder.claim_status`` rolls **any** ``Not-Met`` up to DEFECT
      (it is status-string driven, not disposition driven), so a routing branch
      that merely "did not apply" shows as the "Step 10 No Claim in History
      Chart" DEFECT the auditor sees.

  (B) EVAL side — the denial-is-proper confirmation
        rule:   step:7:10:3  "New Day claim/line denying for timely filing,"
        action: "Not received within the timely filing limit, allow the system
                 to deny for timely filing."
        decision_type: DENY
      This step CONFIRMS the timely-filing denial is correct — a valid claim
      disposition, NOT a processor error — but it carries decision_type=DENY,
      which the run aggregator counts as an adverse finding and sets
      ``RuleExecutionRun.final_decision_type = DENY``.

Together they make a correctly-adjudicated, history-free timely-filing denial
render as a DEFECT (DENY) instead of CLEAN (ALLOW).

What this script does (backfill, no LLM, idempotent)
----------------------------------------------------
For each targeted run whose ONLY defect is this step-10 false positive:
  1. Flip the ``step:7:10:3`` "allow the system to deny for timely filing"
     eval(s) to skipped / Not-Applicable (removes the eval-side DENY).
  2. Flip the Timely-Filing **Not-Met CONDITIONAL** step-10 "No claim in
     history / Kill-Delete Reroute" trace entrie(s) to Skipped / Not-Applicable
     (removes the trace-side DEFECT).
  3. Recompute ``RuleExecutionRun.final_decision_type`` from the surviving
     evals, ``ClaimTrace.final_status`` + ``explainability_json`` from the
     patched trace (via the real ``trace_builder`` functions), and flip the
     executive-summary "Step 10 No Claim in History" line + verdict/headline.

Safety
------
STRICT guard: the change is committed ONLY if, after patching, the run recomputes
to ``final_decision_type == ALLOW`` **and** ``final_status == CLEAN``. If the
claim has any OTHER real finding (so it would not become fully clean), the whole
transaction is rolled back and the claim is left completely untouched — this
script can only ever turn a claim whose SOLE defect is this step-10 false
positive from DEFECT → CLEAN, never the reverse.

Idempotent: a run already cleared (marker present, no residual step-10 Not-Met)
is left alone.

DB target defaults to PROD Postgres (any PG_* env var overrides — the local
prod-replica works out of the box). ``--dry-run`` (default) previews; ``--apply``
writes.

Usage (prod box — bare run uses baked-in prod PG):
    # verify the flagged claim first
    python scripts/fix_tf_step10_no_history_defect_prod.py --claim 25XJ83640500 --dry-run
    python scripts/fix_tf_step10_no_history_defect_prod.py --claim 25XJ83640500 --apply
    # then the whole workflow (all same-signature claims)
    python scripts/fix_tf_step10_no_history_defect_prod.py --dry-run
    python scripts/fix_tf_step10_no_history_defect_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend LLM_BACKEND=none \
    python scripts/fix_tf_step10_no_history_defect_prod.py --dry-run
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

# Adverse dispositions (same set the aggregator + dashboard rollup use).
_ADVERSE = {"DENY", "STOP", "REFER", "REFERRAL", "PEND", "PENDED", "BYPASS"}
_DEFECT = {"DENY", "STOP", "REFER", "REFERRAL", "PEND", "PENDED"}
_PRECEDENCE = ["DENY", "STOP", "PEND", "PENDED", "REFER", "REFERRAL"]

# The two rows that make a correctly-denied, history-free TF claim look defective.
_CONFIRM_DENY_KEY = "step:7:10:3"                 # "allow the system to deny for TF"
_TF_SOP_HINT = "timely_filing"                    # matches sop_name (case-insensitive)

# Unique marker so re-runs are idempotent and the fix is auditable.
_FIX_MARKER = "auditor-fix: TF step-10 no-history — denial proper, no defect (clean)"

_EVAL_NA_MSG = (
    "Timely-filing denial confirmed proper. This is a New-Day claim/line "
    "correctly denying for timely filing (not received within the filing "
    "limit), and there is no matching claim in history to override it — so the "
    "denial is a valid claim disposition, not an audit finding. Marked Not "
    "Applicable. [" + _FIX_MARKER + "]"
)
_TRACE_NA_MSG = (
    "Step 10 (No Claim in History): there is no matching claim/line in history "
    "and this is not a Freq 7/8 resubmission, so the OBH Facets Kill-Delete "
    "Reroute branch does not apply. This is a non-applicable routing branch, "
    "not a defect — the timely-filing denial stands and the claim is clean. "
    "Marked Not Applicable. [" + _FIX_MARKER + "]"
)


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _load_claim_ids(paths: list[str], inline: list[str]) -> set[str]:
    ids: set[str] = set(c.strip() for c in inline if c.strip())
    for path in paths:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                tok = line.split(",")[0].strip().strip('"').strip()
                if not tok or tok.lower() in ("claim", "claim_id", "claimid"):
                    continue
                ids.add(tok)
    return ids


def _is_tf(entry: dict) -> bool:
    return _TF_SOP_HINT in str(entry.get("sop_name") or "").lower()


# The Timely-Filing step-10 branches that are NOT audit findings on a
# correctly-denied, history-free claim. Both render as ``Not-Met`` and get
# wrongly rolled up to DEFECT by ``claim_status``:
#   * the "No claim in history / Kill-Delete Reroute" routing branch (the
#     branch simply did not apply), and
#   * the "allow the system to deny for timely filing" confirmation that the
#     New-Day timely-filing denial is proper (a valid disposition, tagged DENY).
_TF_STEP10_PHRASES = (
    "kill-delete reroute",
    "no claim in history",
    "no claim/line in history",
    "allow the system to deny for timely filing",
    "new day claim/line denying for timely filing",
)


def _is_tf_step10_nonaudit_notmet(entry: dict) -> bool:
    """A Timely-Filing step-10 non-audit branch rendered Not-Met — either the
    'no claim in history / Kill-Delete Reroute' routing branch or the
    'allow the system to deny for timely filing' denial-is-proper confirmation.
    Neither is a processor error, so a Not-Met here is a false defect."""
    from execution_app import trace_builder

    if not _is_tf(entry):
        return False
    if str(entry.get("status") or "") != trace_builder.NOT_MET:
        return False
    hay = " ".join(
        str(entry.get(k) or "")
        for k in ("sop_action", "sop_step_name", "sop_step_description")
    ).lower()
    for sr in entry.get("subrule_results") or []:
        for c in sr.get("conditions") or []:
            hay += " " + str(c.get("condition") or "").lower()
    return any(p in hay for p in _TF_STEP10_PHRASES)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Clear the false Timely-Filing 'Step 10 No Claim in "
        "History' defect on correctly-denied, history-free claims. No LLM."
    )
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID,
                    help=f"Workflow id (default {DEFAULT_WORKFLOW_ID}).")
    ap.add_argument("--claim", action="append", default=[],
                    help="Only these claim id(s) (repeatable).")
    ap.add_argument("--claims-file", action="append", default=[],
                    help="File with claim ids (one per line / CSV first column).")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap claims processed (0 = all).")
    ap.add_argument("--skip-exec-summary", action="store_true",
                    help="Skip updating the executive summary.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview only; write nothing (default).")
    ap.add_argument("--apply", action="store_true", help="Commit the changes.")
    opts = ap.parse_args()
    dry = not opts.apply

    for key, val in _PROD_ENV.items():
        os.environ.setdefault(key, val)
    os.environ["NO_LLM"] = "1"
    os.environ["LLM_BACKEND"] = "none"
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    import django

    django.setup()

    from django.db import transaction

    from execution_app import trace_builder
    from execution_app.models import (
        ClaimExecutiveSummary,
        ClaimTrace,
        RuleEvaluation,
        RuleExecutionRun,
    )
    from execution_app.trace_builder import _build_explainability, _iso

    _p("── Clear false Timely-Filing 'Step 10 No Claim in History' defect ──")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")

    def _recompute_final(evals: list) -> str:
        adverse = [
            ev for ev in evals
            if ev.matched and not ev.skipped
            and (ev.decision_type or "").upper() in _DEFECT
        ]
        if not adverse:
            return "ALLOW"

        def _rank(ev):
            dt = (ev.decision_type or "").upper()
            return _PRECEDENCE.index(dt) if dt in _PRECEDENCE else 99

        return (sorted(adverse, key=_rank)[0].decision_type or "DENY").upper()

    class _Skip(Exception):
        """Raised to roll back + leave the claim untouched (not our signature)."""

    def _fix_one(run: RuleExecutionRun) -> tuple[bool, str]:
        evals = list(RuleEvaluation.objects.filter(run=run))

        # (B) eval side: the "allow the system to deny for TF" confirmation(s).
        confirm = [
            ev for ev in evals
            if ev.rule_key == _CONFIRM_DENY_KEY
            and ev.matched and not ev.skipped
            and (ev.decision_type or "").upper() in _DEFECT
        ]

        ct = ClaimTrace.objects.filter(run=run).first()
        trace = ct.trace_json if (ct and isinstance(ct.trace_json, list)) else []

        # (A) trace side: the step-10 "no history" Not-Met routing branch(es).
        notmet_all = [
            e for e in trace
            if str(e.get("status") or "") == trace_builder.NOT_MET
        ]
        notmet_target = [e for e in notmet_all if _is_tf_step10_nonaudit_notmet(e)]

        prev_status = ct.final_status if ct else None
        prev_final = run.final_decision_type

        # Idempotency: nothing left to change.
        already_eval = all(
            (ev.skipped and _FIX_MARKER in (ev.reasoning or "")) for ev in confirm
        ) if confirm else True
        if not confirm and not notmet_target and already_eval:
            return False, "no TF step-10 false-defect signature (idempotent)"

        # Guard #1: every Not-Met in the trace must be our step-10 branch, else
        # the claim has another real defect -> leave it untouched.
        if any(e not in notmet_target for e in notmet_all):
            return False, "other Not-Met finding present — left untouched"

        touched_shape_ids: set[str] = set()

        # 1) eval flips.
        eval_upd: list = []
        for ev in confirm:
            if ev.skipped and _FIX_MARKER in (ev.reasoning or ""):
                continue
            ev.skipped = True
            ev.matched = False
            ev.verdict = ""
            ev.decision_type = "CONDITIONAL"
            ev.skip_reason = ("not-applicable: TF denial proper, no history — "
                              + _FIX_MARKER)[:255]
            ev.reasoning = _EVAL_NA_MSG
            eval_upd.append(ev)

        # 2) trace flips.
        tchanged = False
        for entry in notmet_target:
            touched_shape_ids.add(str(entry.get("shape_id") or ""))
            entry["status"] = trace_builder.SKIPPED_RULE
            entry["step_exec_status"] = "skipped"
            entry["decision_type"] = "CONDITIONAL"
            entry["rationale"] = _TRACE_NA_MSG
            for sr in entry.get("subrule_results") or []:
                sr["status"] = trace_builder.SKIPPED_RULE
                if not sr.get("statement"):
                    sr["statement"] = _TRACE_NA_MSG
            tchanged = True

        # 3) recompute derived status from the (in-memory) patched rows.
        new_final = _recompute_final(evals)
        new_status = trace_builder.claim_status(trace) if trace else prev_status

        # Guard #2: only commit a fully-clean result.
        if new_final != "ALLOW" or (trace and new_status != trace_builder.CLEAN):
            raise _Skip(
                f"would not become clean (final={new_final}, status={new_status})"
            )

        if not (eval_upd or tchanged):
            return False, "already clean (idempotent)"

        if dry:
            return True, (
                f"{len(eval_upd)} eval + {len(notmet_target)} trace step-10 "
                f"row(s) -> N/A; {prev_final}->{new_final} / "
                f"{prev_status}->{new_status}"
            )

        # ---- writes ----
        if eval_upd:
            RuleEvaluation.objects.bulk_update(
                eval_upd,
                ["skipped", "matched", "verdict", "decision_type",
                 "skip_reason", "reasoning"],
            )

        if run.final_decision_type != new_final:
            run.final_decision_type = new_final
            run.save(update_fields=["final_decision_type"])

        if ct is not None and (tchanged or ct.final_status != new_status):
            ct.final_status = trace_builder.claim_status(ct.trace_json)
            ct.explainability_json = _build_explainability(
                ct.trace_json, str(run.id), run.claim_id,
                _iso(run.started_at), _iso(run.finished_at), run,
            )
            ct.save(update_fields=[
                "trace_json", "explainability_json", "final_status", "updated_at",
            ])

        # 4) executive summary — flip the step-10 line + verdict/headline.
        if not opts.skip_exec_summary:
            es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
            if es is not None:
                fields: list[str] = []
                steps = list(es.step_summaries or [])
                sfixed = False
                for st in steps:
                    if not isinstance(st, dict):
                        continue
                    sid = str(st.get("shape_id") or "")
                    hay = f"{st.get('summary', '')} {st.get('agent_name', '')}".lower()
                    hit = (sid and sid in touched_shape_ids) or (
                        st.get("status") == "DEFECT"
                        and ("no claim in history" in hay
                             or "no claim/line in history" in hay
                             or "kill-delete reroute" in hay)
                    )
                    if not hit:
                        continue
                    if st.get("status") != "NOT_APPLICABLE":
                        st["status"] = "NOT_APPLICABLE"
                        st["summary"] = _TRACE_NA_MSG
                        sfixed = True
                if sfixed:
                    es.step_summaries = steps
                    fields.append("step_summaries")
                if es.verdict != new_final:
                    es.verdict = new_final
                    fields.append("verdict")
                if es.audit_status != "CLEAN":
                    es.audit_status = "CLEAN"
                    fields.append("audit_status")
                new_head = (
                    f"Claim {run.claim_id} is clean — no defect (ALLOW). "
                    "The timely-filing denial was correctly applied and there "
                    "is no claim in history; the Step 10 branch is not "
                    "applicable, not a defect."
                )
                if (es.headline or "").strip() != new_head:
                    es.headline = new_head
                    fields.append("headline")
                if fields:
                    if es.generated_by != "backfill":
                        es.generated_by = "backfill"
                        fields.append("generated_by")
                    fields.append("updated_at")
                    es.save(update_fields=fields)

        return True, (
            f"{len(eval_upd)} eval + {len(notmet_target)} trace step-10 row(s) "
            f"-> N/A; {prev_final}->{new_final} / {prev_status}->{new_status}"
        )

    # Latest terminal run per claim in the workflow.
    latest: dict[str, RuleExecutionRun] = {}
    for run in RuleExecutionRun.objects.filter(workflow_id=opts.workflow).order_by(
        "claim_id", "-started_at"
    ):
        if run.claim_id and run.claim_id not in latest:
            latest[run.claim_id] = run

    wanted = _load_claim_ids(opts.claims_file, opts.claim)
    if wanted:
        missing = sorted(wanted - set(latest))
        latest = {c: r for c, r in latest.items() if c in wanted}
        _p(f"\n  claim filter  = {len(wanted)} id(s); {len(latest)} matched, "
           f"{len(missing)} not found")
        for m in missing:
            _p(f"    NOT FOUND: {m}")

    claim_ids = sorted(latest)
    if opts.limit:
        claim_ids = claim_ids[: opts.limit]
    total = len(claim_ids)
    _p(f"\n══ backfill {total} run(s) ══")

    changed = skipped = failed = 0
    for i, cid in enumerate(claim_ids, 1):
        run = latest[cid]
        try:
            if dry:
                try:
                    did, note = _fix_one(run)
                except _Skip as sk:
                    did, note = False, str(sk)
            else:
                try:
                    with transaction.atomic():
                        did, note = _fix_one(run)
                except _Skip as sk:
                    did, note = False, str(sk)
        except Exception as exc:  # pragma: no cover - defensive
            failed += 1
            _p(f"[{i}/{total}] claim={cid} run={run.id} FAILED: {exc}")
            continue
        if did:
            changed += 1
            _p(f"[{i}/{total}] claim={cid} [CHANGED] {note}")
        else:
            skipped += 1
        if i % 25 == 0 or i == total:
            _p(f"PROGRESS {i}/{total} "
               f"({int(i / total * 100) if total else 100}%)  "
               f"changed={changed} skipped={skipped} failed={failed}")

    _p("────────────────────────────────────────────────────────────")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  scanned  = {total}")
    _p(f"  changed  = {changed}")
    _p(f"  skipped  = {skipped}  (no signature / other finding / idempotent)")
    _p(f"  failed   = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
