#!/usr/bin/env python3
r"""Mark Timely Filing **Step 1** (the "Is your claim/line denying for TF1 or
TF0?" gate) as NOT APPLICABLE on every claim — deterministic, NO LLM, in place.

Why this exists
---------------
The Timely Filing P&P (SOP ``OBH_Facets_Timely_Filing``, sop_id 7) should
confirm timely filing on all claims — it must NOT gate on whether the claim
denied for TF0/TF1. Step 1 is exactly that TF0/TF1-denial gate, so per the
auditors ONLY Step 1 should read **Not Applicable** (and show nothing); every
other Timely Filing step is left exactly as it was evaluated.

Scope: rule rows ``step:7:1:*`` (Step 1) only. All other TF rows
(``step:7:2:*`` … and the ``pre:7:*`` preconditions) are untouched.

This script, per claim's latest terminal run in the workflow:

  1. ``RuleEvaluation`` — each Step 1 row (rule_key ``step:7:1:*``) is marked
     ``skipped=True, matched=False`` with a ``not-applicable:`` skip_reason (NOT
     "out of scope", so trace_builder.scope_category → NOT_APPLICABLE) and a
     BLANK ``reasoning`` (nothing shown in the summary tab).
  2. ``RuleExecutionRun`` — verdict recomputed from the surviving evaluations
     (Step 1 is a non-scoring routing gate, so the verdict normally does not
     change; recompute is defensive + keeps a TF-only halt from sticking).
  3. ``ClaimTrace`` — the Step 1 trace entry (sop_step_number == 1) flipped to
     ``Skipped`` with blank rationale + blank sub-rule statements and tools moved
     to ``tools_skipped``; ``final_status`` + ``explainability_json`` recomputed.
  4. ``ClaimExecutiveSummary`` — verdict/status synced in place if it changed
     (no regen, no LLM). TF step summaries are left intact (the SOP still runs).

Idempotent: a run whose Step 1 rows are already NA is left alone.

DB target defaults to PROD Postgres (any PG_* env var overrides; the local
prod-replica on 127.0.0.1:5433 works out of the box). ``--dry-run`` (default)
previews; ``--apply`` writes.

Usage (prod box):
    python scripts/scope_timely_filing_na_prod.py --dry-run
    python scripts/scope_timely_filing_na_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend LLM_BACKEND=none NO_LLM=1 \
    python scripts/scope_timely_filing_na_prod.py --dry-run
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
_DEFECT = {"DENY", "STOP", "REFER", "REFERRAL", "PEND", "PENDED"}
_PRECEDENCE = ["DENY", "STOP", "PEND", "PENDED", "REFER", "REFERRAL"]

# Only Timely Filing STEP 1 (the TF0/TF1-denial gate) is scoped Not Applicable.
TF_STEP_NUMBER = 1

# skip_reason MUST NOT contain "out of scope" (trace_builder.scope_category keys
# off that phrase). Using the ``not-applicable:`` prefix → NOT_APPLICABLE rollup.
_NA_MARKER = "not-applicable: timely filing step 1"
_NA_REASON = (
    "not-applicable: timely filing step 1 (TF0/TF1 denial gate) not applicable "
    "for this claim"
)

_CLEAN_NARRATIVE = (
    "No auditor defect. Timely Filing Step 1 (TF0/TF1 denial gate) is not "
    "applicable to this claim; all in-scope SOP steps passed."
)


def _is_timely_title(title: str) -> bool:
    return "timely" in (title or "").lower()


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


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Mark the Timely Filing SOP NOT APPLICABLE on every claim; "
        "backfill existing runs. No LLM."
    )
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--claim", action="append", default=[],
                    help="Only these claim id(s) (repeatable).")
    ap.add_argument("--claims-file", action="append", default=[],
                    help="File with claim ids (one per line / CSV first column).")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap number of claims processed (0 = all).")
    ap.add_argument("--skip-exec-summary", action="store_true",
                    help="Skip the in-place executive-summary update.")
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
    from sop_ingestion.models import AuditSop
    from uhc_execution_engine.rule_loader import load_workflow_bindings

    _p("── Mark Timely Filing NOT APPLICABLE (all claims) ─────")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")

    # Resolve the Timely Filing SOP id(s) from the workflow bindings.
    loaded = load_workflow_bindings(opts.workflow)
    tf_sop_ids: set = set()
    for r in loaded["decisions"] + loaded["preconditions"]:
        if _is_timely_title(r.get("sop_title")):
            tf_sop_ids.add(r.get("sop_id"))
    if not tf_sop_ids:
        sys.exit("ERROR: no Timely Filing SOP found in this workflow.")
    tf_titles = {
        (s.title or "")
        for s in AuditSop.objects.filter(id__in=tf_sop_ids).only("title")
    }
    # Step 1 only: rule_key == step:{sop}:1:{row}
    key_prefixes = tuple(f"step:{sid}:{TF_STEP_NUMBER}:" for sid in tf_sop_ids)
    _p(f"  Timely Filing SOP id(s) = {sorted(tf_sop_ids)}  (Step {TF_STEP_NUMBER} only)")
    for t in sorted(tf_titles):
        _p(f"      • {t}")

    def _is_tf_step1_entry(entry: dict) -> bool:
        if "timely" not in (entry.get("sop_name") or "").lower():
            return False
        return str(entry.get("sop_step_number")) == str(TF_STEP_NUMBER)

    def _seed_one(run: RuleExecutionRun) -> tuple[bool, str]:
        evals = list(RuleEvaluation.objects.filter(run=run))
        tf_evals = [
            e for e in evals if any(e.rule_key.startswith(p) for p in key_prefixes)
        ]
        if not tf_evals:
            return False, "no Timely Filing Step 1 evaluations on this run"

        # 1) Mark every Step 1 row skipped=NA with BLANK reasoning. Batched write.
        eval_changed = False
        to_update: list = []
        for ev in tf_evals:
            already = (
                ev.skipped
                and not ev.matched
                and _NA_MARKER in (ev.skip_reason or "")
                and not (ev.reasoning or "")
            )
            if already:
                continue
            eval_changed = True
            ev.skipped = True
            ev.matched = False
            ev.verdict = ""
            ev.skip_reason = _NA_REASON[:255]
            ev.reasoning = ""
            to_update.append(ev)
        if to_update and not dry:
            RuleEvaluation.objects.bulk_update(
                to_update,
                ["skipped", "matched", "verdict", "skip_reason", "reasoning"],
            )
        # keep in-memory list consistent for the verdict recompute below
        for ev in tf_evals:
            ev.skipped = True
            ev.matched = False

        # 2) Recompute the run verdict from the surviving (non-skipped) evals.
        adverse = [
            ev for ev in evals
            if ev.matched and not ev.skipped
            and (ev.decision_type or "").upper() in _DEFECT
        ]
        if adverse:
            def _rank(ev):
                dt = (ev.decision_type or "").upper()
                return _PRECEDENCE.index(dt) if dt in _PRECEDENCE else 0

            winner = sorted(adverse, key=_rank)[0]
            final = (winner.decision_type or "DENY").upper()
            codes: list[str] = []
            for ev in adverse:
                for c in ev.codes or []:
                    if c not in codes:
                        codes.append(c)
            narrative = run.narrative
            new_status = run.status
        else:
            final, codes, narrative = "ALLOW", [], _CLEAN_NARRATIVE
            new_status = "COMPLETED"

        verdict_changed = (
            run.final_decision_type != final
            or (run.status or "") != new_status
        )
        if not dry and (eval_changed or verdict_changed):
            run.final_decision_type = final
            run.applied_codes = codes
            run.narrative = narrative
            run.status = new_status
            run.save(update_fields=[
                "final_decision_type", "applied_codes", "narrative", "status",
            ])
        run.final_decision_type = final

        # 3) Patch the stored trace: flip the Step 1 entry to Skipped + blank text.
        tchanged = False
        ct = ClaimTrace.objects.filter(run=run).first()
        if ct and isinstance(ct.trace_json, list):
            for entry in ct.trace_json:
                if not _is_tf_step1_entry(entry):
                    continue
                needs = (
                    entry.get("status") != trace_builder.SKIPPED_RULE
                    or (entry.get("rationale") or "")
                    or (entry.get("tools_used") or [])
                    or any(
                        (sr.get("statement") or "")
                        or sr.get("status") != trace_builder.SKIPPED_RULE
                        for sr in (entry.get("subrule_results") or [])
                    )
                )
                if not needs:
                    continue
                entry["status"] = trace_builder.SKIPPED_RULE
                entry["step_exec_status"] = "skipped"
                entry["rationale"] = ""
                used = list(entry.get("tools_used") or [])
                if used:
                    entry["tools_skipped"] = sorted(
                        set(list(entry.get("tools_skipped") or []) + used)
                    )
                entry["tools_used"] = []
                entry["tools_succeeded"] = []
                entry["tools_failed"] = []
                for sr in entry.get("subrule_results") or []:
                    sr["status"] = trace_builder.SKIPPED_RULE
                    sr["statement"] = ""
                tchanged = True
            if tchanged and not dry:
                ct.final_status = trace_builder.claim_status(ct.trace_json)
                ct.explainability_json = _build_explainability(
                    ct.trace_json, str(run.id), run.claim_id,
                    _iso(run.started_at), _iso(run.finished_at), run,
                )
                ct.save(update_fields=[
                    "trace_json", "explainability_json", "final_status", "updated_at",
                ])

        changed = eval_changed or verdict_changed or tchanged

        # 4) Sync the executive summary verdict/status IN PLACE if it changed
        # (NO regen, NO LLM). TF step summaries are left intact — the SOP still
        # runs; only its Step 1 gate became Not Applicable.
        if verdict_changed and not opts.skip_exec_summary and not dry:
            es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
            if es is not None:
                fields: list[str] = []
                if es.verdict != final:
                    es.verdict = final
                    fields.append("verdict")
                new_es_status = "CLEAN" if final == "ALLOW" else es.audit_status
                if es.audit_status != new_es_status:
                    es.audit_status = new_es_status
                    fields.append("audit_status")
                if fields:
                    fields.append("updated_at")
                    es.save(update_fields=fields)

        if not changed:
            return False, "already NA (idempotent)"
        n = len(tf_evals)
        return True, f"{n} Timely Filing Step 1 row(s) -> NOT_APPLICABLE; verdict {final}"

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

    claim_ids = sorted(latest)
    if opts.limit:
        claim_ids = claim_ids[: opts.limit]
    total = len(claim_ids)
    _p(f"\n══ Backfill {total} run(s) ══")

    changed = skipped = failed = 0
    for i, cid in enumerate(claim_ids, 1):
        run = latest[cid]
        try:
            if dry:
                did, note = _seed_one(run)
            else:
                with transaction.atomic():
                    did, note = _seed_one(run)
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
            _p(f"PROGRESS {i}/{total} ({int(i / total * 100) if total else 100}%)  "
               f"changed={changed} skipped={skipped} failed={failed}")

    _p("────────────────────────────────────────────────────────────")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  scanned  = {total}")
    _p(f"  changed  = {changed}")
    _p(f"  skipped  = {skipped}  (already NA / no TF evals)")
    _p(f"  failed   = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
