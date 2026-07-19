#!/usr/bin/env python3
r"""Scope the Medicare-only SOPs (NPI check + Provider Opt-Out) to Medicare
claims only — deterministically, NO LLM.

Why this exists
---------------
The audit workflow chains several SOPs. Two of them are Medicare-specific:

  * SOP "Provider Name and NPI Validation Audit Guidelines"  (the NPPES/NPI check)
  * SOP "Provider Opt-Out Look-Up Audit Guidelines"          (Medicare opt-out)

Per the SOP flowchart these only apply after "Is the Plan Medicare? → Yes".
But they carried no LOB scope, so they ran for EVERY claim — including
Commercial/Medicaid — which is wrong (an auditor flagged a Medicare tool being
called on a Commercial claim). LOB is already derived per claim
(``determine_claim_lob`` → ``RuleExecutionRun.claim_lob``); this script simply
puts it to work.

Two phases, both no-LLM:

  Phase A — WORKFLOW SCOPING (forward fix, once)
      Sets ``Workbench.config['lob_scope'] = ['Medicare']`` on the two SOP
      columns. The engine ALREADY honours this (``_workbench_lob_scope`` →
      ``execute_shapes._rule_in_lob_scope``): every future non-Medicare claim
      skips those SOPs' rules with NO LLM call and the UI greys them.

  Phase B — BACKFILL EXISTING RUNS (claim by claim, no LLM)
      For each already-executed run whose LOB is NOT Medicare, retro-marks the
      two SOPs as skipped so the stored result matches the new rule:
        1. ``RuleEvaluation`` rows (rule_key ``step:<sop>:*``) -> ``skipped=True``,
           ``matched=False`` with an LOB skip_reason (excluded from verdict,
           greyed in the UI).
        2. ``RuleExecutionRun`` verdict recomputed from the surviving
           (non-skipped) evaluations — dashboard-rollup semantics.
        3. ``ClaimTrace`` — the two SOPs' trace entries flipped to ``Skipped``
           (tools moved to ``tools_skipped``); ``final_status`` +
           ``explainability_json`` recomputed.
        4. ``ClaimExecutiveSummary`` updated IN PLACE (NO LLM, NO regen): the
           verdict is set and the two Medicare-only ``step_summaries`` lines are
           flipped to ``OUT_OF_SCOPE`` with the LOB message. (The old full
           regen did ~233 SQL round trips per claim — minutes each over the prod
           link; the in-place update is ~1 query.)

Medicare claims are left untouched (those SOPs legitimately run for them).

Idempotent: a run whose two SOPs are already skipped is left alone.

DB target defaults to PROD Postgres (any PG_* env var overrides — the local
prod-replica works out of the box). ``--dry-run`` (default) previews; ``--apply``
writes.

Usage (prod box — bare run uses baked-in prod PG):
    python scripts/scope_medicare_steps_prod.py --dry-run
    python scripts/scope_medicare_steps_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend LLM_BACKEND=none \
    python scripts/scope_medicare_steps_prod.py --dry-run
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

MEDICARE_PRODUCT = "Medicare"
LOB_SCOPE = ["Medicare"]

# Adverse dispositions (same set the aggregator + dashboard rollup use).
_DEFECT = {"DENY", "STOP", "REFER", "REFERRAL", "PEND", "PENDED"}
_PRECEDENCE = ["DENY", "STOP", "PEND", "PENDED", "REFER", "REFERRAL"]

_CLEAN_NARRATIVE = (
    "No auditor defect. Medicare-only checks (Provider NPI validation and "
    "Provider Opt-Out look-up) are out of scope for this non-Medicare claim and "
    "were skipped; all in-scope SOP steps passed."
)


def _is_medicare_only_title(title: str) -> bool:
    """True for the two Medicare-only SOPs (NPI validation, Provider Opt-Out)."""
    t = (title or "").lower()
    return ("npi validation" in t) or ("opt-out" in t) or ("opt out" in t)


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _row_is_adverse(decision_type: str, codes) -> bool:
    return (decision_type or "").upper() in _DEFECT


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
        description="Scope Medicare-only SOPs (NPI check + Provider Opt-Out) to "
        "Medicare claims only; backfill existing runs. No LLM."
    )
    ap.add_argument(
        "--workflow",
        default=DEFAULT_WORKFLOW_ID,
        help=f"Workflow id (default {DEFAULT_WORKFLOW_ID}).",
    )
    ap.add_argument(
        "--claim",
        action="append",
        default=[],
        help="Only these claim id(s) (repeatable).",
    )
    ap.add_argument(
        "--claims-file",
        action="append",
        default=[],
        help="File with claim ids (one per line / CSV first column).",
    )
    ap.add_argument(
        "--limit", type=int, default=0, help="Cap number of claims processed (0 = all)."
    )
    ap.add_argument(
        "--skip-workflow-scope",
        action="store_true",
        help="Skip Phase A (assume the SOP lob_scope is already set).",
    )
    ap.add_argument(
        "--skip-exec-summary",
        action="store_true",
        help="Skip regenerating the executive summary in Phase B.",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Preview only; write nothing (default)."
    )
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

    from builder.models import Workbench
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

    _p("── Scope Medicare-only SOPs (NPI + Opt-Out) to Medicare ─────")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")

    # Resolve the Medicare-only SOP id(s) + titles from the workflow bindings.
    loaded = load_workflow_bindings(opts.workflow)
    med_sop_ids: set = set()
    for r in loaded["decisions"]:
        if _is_medicare_only_title(r.get("sop_title")):
            med_sop_ids.add(r.get("sop_id"))
    if not med_sop_ids:
        sys.exit(
            "ERROR: no Medicare-only SOP (NPI validation / Opt-Out) found "
            "in this workflow."
        )
    med_titles = {
        (s.title or "")
        for s in AuditSop.objects.filter(id__in=med_sop_ids).only("title")
    }
    med_titles_lower = {t.lower() for t in med_titles}
    key_prefixes = tuple(f"step:{sid}:" for sid in med_sop_ids)
    _p(f"  Medicare-only SOP id(s) = {sorted(med_sop_ids)}")
    for t in sorted(med_titles):
        _p(f"      • {t}")

    # ── Phase A — workflow scoping (forward) ──────────────────────────────────
    if not opts.skip_workflow_scope:
        _p("\n══ Phase A — set lob_scope=['Medicare'] on the two SOP columns ══")
        wbs = Workbench.objects.filter(work_area__workflow_id=opts.workflow)
        touched = 0
        for wb in wbs:
            cfg = dict(wb.config or {})
            if cfg.get("sop_id") in med_sop_ids:
                cur = cfg.get("lob_scope") or []
                if cur != LOB_SCOPE:
                    cfg["lob_scope"] = list(LOB_SCOPE)
                    _p(
                        f"    workbench {wb.id} (sop_id={cfg.get('sop_id')}) "
                        f"lob_scope {cur!r} -> {LOB_SCOPE!r}"
                    )
                    if not dry:
                        wb.config = cfg
                        wb.save(update_fields=["config"])
                    touched += 1
        _p(f"    workbenches updated: {touched}")

    # ── Phase B — backfill existing non-Medicare runs ─────────────────────────
    # ``skip_reason`` MUST start with "out of scope" so the dashboard rollup
    # (trace_builder.scope_category) classifies the SOP as OUT_OF_SCOPE. The
    # longer ``reasoning`` is the human line the UI shows on the (greyed) step.
    _LOB_MARKER = "did not run"
    # step_summaries lines that belong to the two Medicare-only SOPs, matched by
    # keyword against the step's agent_name/summary (deterministic, no LLM).
    _MED_STEP_KEYWORDS = ("npi", "opt-out", "opt out", "provider opt")

    def _skip_reason(label: str) -> str:
        return (
            f"out of scope: LOB is {label} (non-Medicare) — Medicare-only "
            "step (Provider NPI validation / Provider Opt-Out) not executed"
        )

    def _lob_message(label: str) -> str:
        return (
            f"Line of Business is {label} (non-Medicare). Provider NPI "
            "validation and Provider Opt-Out look-up are Medicare-only "
            "checks, so this step is out of scope for this claim and did "
            "not run."
        )

    def _seed_one(run: RuleExecutionRun) -> tuple[bool, str]:
        claim_lob = run.claim_lob or {}
        product = str(claim_lob.get("product") or "").strip()
        label = str(claim_lob.get("label") or product or "unknown")
        if product == MEDICARE_PRODUCT:
            return False, "Medicare claim — SOPs legitimately run"

        reason = _skip_reason(label)
        msg = _lob_message(label)
        evals = list(RuleEvaluation.objects.filter(run=run))
        med_evals = [
            e for e in evals if any(e.rule_key.startswith(p) for p in key_prefixes)
        ]
        if not med_evals:
            return False, "no NPI/Opt-Out evaluations on this run"

        eval_changed = False
        # 1) Mark the Medicare-only evals skipped, and REWRITE reasoning to the
        # LOB message (the summary tab surfaces reasoning, so the old
        # "NPI validated…" text must not survive). Each row is guarded so the
        # step is idempotent independently of the trace/summary below.
        # Writes are batched into a single bulk_update — the previous per-row
        # save() flooded the prod link with round trips.
        to_update: list = []
        for ev in med_evals:
            if ev.skipped and _LOB_MARKER in (ev.reasoning or ""):
                continue
            eval_changed = True
            ev.skipped = True
            ev.matched = False
            ev.verdict = ""
            ev.skip_reason = reason[:255]
            ev.reasoning = msg
            to_update.append(ev)
        if to_update and not dry:
            RuleEvaluation.objects.bulk_update(
                to_update,
                ["skipped", "matched", "verdict", "skip_reason", "reasoning"],
            )

        # 2) Recompute the run verdict from the surviving (non-skipped) evals.
        adverse = [
            ev
            for ev in evals
            if ev.matched
            and not ev.skipped
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
        else:
            final, codes, narrative = "ALLOW", [], _CLEAN_NARRATIVE
        verdict_changed = run.final_decision_type != final
        if not dry and (eval_changed or verdict_changed):
            run.final_decision_type = final
            run.applied_codes = codes
            run.narrative = narrative
            run.save(
                update_fields=["final_decision_type", "applied_codes", "narrative"]
            )
        run.final_decision_type = final

        # 3) Patch the stored trace: flip the two SOPs' entries to Skipped.
        tchanged = False
        ct = ClaimTrace.objects.filter(run=run).first()
        if ct and isinstance(ct.trace_json, list):
            for entry in ct.trace_json:
                if (entry.get("sop_name") or "").lower() not in med_titles_lower:
                    continue
                if entry.get(
                    "status"
                ) != trace_builder.SKIPPED_RULE or _LOB_MARKER not in str(
                    entry.get("rationale") or ""
                ):
                    entry["status"] = trace_builder.SKIPPED_RULE
                    entry["step_exec_status"] = "skipped"
                    entry["rationale"] = msg
                    # The Medicare tools were not applicable — reflect that.
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
                        sr["statement"] = msg
                    tchanged = True
            if tchanged and not dry:
                ct.final_status = trace_builder.claim_status(ct.trace_json)
                ct.explainability_json = _build_explainability(
                    ct.trace_json,
                    str(run.id),
                    run.claim_id,
                    _iso(run.started_at),
                    _iso(run.finished_at),
                    run,
                )
                ct.save(
                    update_fields=[
                        "trace_json",
                        "explainability_json",
                        "final_status",
                        "updated_at",
                    ]
                )

        changed = eval_changed or verdict_changed or tchanged

        # 4) Update the executive summary IN PLACE (NO regen, NO LLM). The old
        # executive_summary.generate_for_run() re-collected every step/tool of
        # the run (~233 SQL round trips per claim) which, over the prod link,
        # made the backfill take minutes per claim. We only need to reflect the
        # new verdict and flip the two Medicare-only step lines out of scope.
        if changed and not opts.skip_exec_summary and not dry:
            es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
            if es is not None:
                fields: list[str] = []
                if es.verdict != final:
                    es.verdict = final
                    fields.append("verdict")
                new_status = "CLEAN" if final == "ALLOW" else es.audit_status
                if es.audit_status != new_status:
                    es.audit_status = new_status
                    fields.append("audit_status")
                steps = list(es.step_summaries or [])
                sfixed = False
                for st in steps:
                    if not isinstance(st, dict):
                        continue
                    hay = f"{st.get('agent_name', '')} {st.get('summary', '')}".lower()
                    if not any(k in hay for k in _MED_STEP_KEYWORDS):
                        continue
                    if st.get("summary") != msg or st.get("status") != "OUT_OF_SCOPE":
                        st["status"] = "OUT_OF_SCOPE"
                        st["summary"] = msg
                        sfixed = True
                if sfixed:
                    es.step_summaries = steps
                    fields.append("step_summaries")
                if es.generated_by != "backfill":
                    es.generated_by = "backfill"
                    fields.append("generated_by")
                if fields:
                    fields.append("updated_at")
                    es.save(update_fields=fields)

        if not changed:
            return False, "already scoped (idempotent)"
        n = sum(1 for e in med_evals)
        return True, f"{n} NPI/Opt-Out evals skipped ({label}) -> verdict {final}"

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
        _p(
            f"\n  claim filter  = {len(wanted)} id(s); {len(latest)} matched, "
            f"{len(missing)} not found"
        )

    claim_ids = sorted(latest)
    if opts.limit:
        claim_ids = claim_ids[: opts.limit]
    total = len(claim_ids)
    _p(f"\n══ Phase B — backfill {total} run(s) ══")

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
            _p(
                f"PROGRESS {i}/{total} ({int(i / total * 100) if total else 100}%)  "
                f"changed={changed} skipped={skipped} failed={failed}"
            )

    _p("────────────────────────────────────────────────────────────")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  scanned  = {total}")
    _p(f"  changed  = {changed}")
    _p(f"  skipped  = {skipped}  (Medicare / already scoped / no evals)")
    _p(f"  failed   = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
