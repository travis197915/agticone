#!/usr/bin/env python3
r"""Mark "process action" steps as NOT APPLICABLE across all SOPs —
deterministically, NO LLM.

Why this exists  (tracker item #19 — "Status Across all SOPs")
--------------------------------------------------------------
Some SOP steps are not audit *checks* — they are a claim-**processing action**
the adjudicator keys once the audit is clean, e.g. the terminal

    "(F3) Process the claim."

step at the end of the Duplicate-Claim-Handling SOP. The engine currently
evaluates that step and marks it **Met** (an ALLOW), so the UI shows a green
"Met" chip on what is really just a "now process it" instruction. Auditors want
these process-action steps shown as **Not Applicable** — they are neither a pass
nor a defect, they are simply out of the audit's remit.

What counts as a "process action" (the F3/F4 keystroke macros)
-------------------------------------------------------------
A step qualifies when its decision_type is NON-adverse (never
DENY/STOP/REFER/PEND/BYPASS — those are real findings, left untouched) AND its
action text is a Facets **keystroke macro**:
  * it references an F3/F4 keystroke  (``\(?F[34]\)?`` — NOT F5/F24/F51, which
    are pend/EOB codes; ``<Shift + F4>`` is included), AND EITHER
  * it carries a process/save keystroke command
    (``process`` | ``save`` | ``accept`` | ``continue`` | ``File >``), OR
  * the action is just the bare keystroke token itself ("(F3)", "(F4)",
    "<Shift + F4>").

On the target workflow this isolates exactly these terminal keystroke actions,
all ALLOW/CONDITIONAL, with no false positives:
  * "(F3) Process the claim."                       (Duplicate-Claim-Handling)
  * "(F3) (File > Process)"                          (Member-Eligibility step 14)
  * "(F4) Save the claim."   /   "(F3)"   /   "(F4)"
  * "<Shift + F4> (File > Save > Accept/Continue)"
It deliberately does NOT match audit steps that merely mention "process" without
a keystroke (e.g. "Process both the current claim and the claim in history").

How "Not Applicable" is represented (no schema change, no LLM)
-------------------------------------------------------------
For each matching row we set the ``RuleEvaluation`` to ``skipped=True,
matched=False`` with a ``skip_reason`` that does NOT start with "out of scope"
— so ``trace_builder.scope_category`` classifies it as NOT_APPLICABLE (not
OUT_OF_SCOPE). The stored ``ClaimTrace`` step is flipped to ``Skipped`` (which
the dashboard's ``normalizeAuditStatus`` maps to NOT_APPLICABLE) with the N/A
rationale, and the executive-summary step line is flipped IN PLACE. Because
these steps are their own shape/agent, the agent chip + group header roll up to
NOT APPLICABLE automatically.

Verdict safety: only NON-adverse rows are skipped, so the claim's aggregate
verdict (and CLEAN/DEFECT status) is recomputed but never regresses — a clean
claim stays clean.

Forward-fix (NEW): the execution engine now skips process-action steps
automatically on every run — see ``is_process_action`` in
``uhc-execution-engine/.../agents/n_execute_shapes.py`` (Pass-1 deterministic
skips, NO LLM). This script remains the BACKFILL for runs produced BEFORE that
forward-fix landed. Both use the identical detector, so they agree row-for-row.

One phase, no-LLM:
  Phase B — BACKFILL EXISTING RUNS (claim by claim)   [there is no Phase A: this
      is a per-step reclassification, not an LOB gate, so there is no workflow
      config to set.]

Idempotent: a run whose process-action steps are already N/A is left alone.

DB target defaults to PROD Postgres (any PG_* env var overrides — the local
prod-replica works out of the box). ``--dry-run`` (default) previews; ``--apply``
writes.

Usage (prod box — bare run uses baked-in prod PG):
    python scripts/mark_process_action_na_prod.py --dry-run
    python scripts/mark_process_action_na_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend LLM_BACKEND=none \
    python scripts/mark_process_action_na_prod.py --dry-run
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

# Adverse dispositions (same set the aggregator + dashboard rollup use). A
# process-action reclassification must NEVER touch a real finding row.
_ADVERSE = {"DENY", "STOP", "REFER", "REFERRAL", "PEND", "PENDED", "BYPASS"}
_PRECEDENCE = ["DENY", "STOP", "PEND", "PENDED", "REFER", "REFERRAL"]

# "process action" detector — a Facets F3/F4 keystroke macro. Matches the six
# terminal-keystroke actions the SOPs use ("(F3) Process the claim.",
# "(F3) (File > Process)", "(F4) Save the claim.", "<Shift + F4> (File > Save >
# Accept/Continue)", bare "(F3)" / "(F4)"). Does NOT match F5/F24/F51 (pend/EOB
# codes) nor audit steps that merely mention "process" without a keystroke.
_FKEY = re.compile(r"\(?\bF[34]\b\)?", re.I)          # F3, (F3), F4, (F4)  — not F5/F24/F51
_CMD = re.compile(r"\b(process|save|accept|continue)\b|file\s*>", re.I)

# Unique marker in the reasoning/rationale so re-runs are idempotent.
_NA_MARKER = "processing action"

_NA_MESSAGE = (
    "This step is a claim-processing keystroke action (e.g. F3 Process / F4 "
    "Save / Shift+F4 Accept), not an audit determination. It is neither a pass "
    "nor a defect, so it is marked Not Applicable."
)


def _is_bare_keystroke(action: str) -> bool:
    """True when the action is nothing but an F3/F4 keystroke token, e.g.
    "(F3)", "(F4)", "<Shift + F4>" — modifiers/punctuation stripped."""
    s = re.sub(r"(?i)\b(shift|ctrl|alt)\b", "", action or "")
    s = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    return s in ("F3", "F4")


def _is_process_action(action: str, decision_type: str) -> bool:
    if (decision_type or "").upper() in _ADVERSE:
        return False
    a = action or ""
    if not _FKEY.search(a):
        return False
    return bool(_CMD.search(a) or _is_bare_keystroke(a))


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
        description="Mark F3/F4 process-action steps as NOT APPLICABLE across "
        "all SOPs; backfill existing runs. No LLM."
    )
    ap.add_argument(
        "--workflow",
        default=DEFAULT_WORKFLOW_ID,
        help=f"Workflow id (default {DEFAULT_WORKFLOW_ID}).",
    )
    ap.add_argument(
        "--claim", action="append", default=[],
        help="Only these claim id(s) (repeatable).",
    )
    ap.add_argument(
        "--claims-file", action="append", default=[],
        help="File with claim ids (one per line / CSV first column).",
    )
    ap.add_argument(
        "--limit", type=int, default=0, help="Cap claims processed (0 = all)."
    )
    ap.add_argument(
        "--skip-exec-summary", action="store_true",
        help="Skip updating the executive summary step line in Phase B.",
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

    from execution_app import trace_builder
    from execution_app.models import (
        ClaimExecutiveSummary,
        ClaimTrace,
        RuleEvaluation,
        RuleExecutionRun,
    )
    from execution_app.trace_builder import _build_explainability, _iso

    _p("── Mark F3/F4 process-action steps NOT APPLICABLE ───────────")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")

    def _seed_one(run: RuleExecutionRun) -> tuple[bool, str]:
        evals = list(RuleEvaluation.objects.filter(run=run))
        proc_evals = [
            e for e in evals if _is_process_action(e.action, e.decision_type)
        ]
        if not proc_evals:
            return False, "no process-action step on this run"

        # 1) Flip the process-action evals to skipped / Not Applicable. Each row
        # is guarded on the marker so the step is idempotent. Batched into one
        # bulk_update to spare the prod link.
        eval_changed = False
        proc_shape_ids: set[str] = set()
        to_update: list = []
        for ev in proc_evals:
            if ev.skipped and _NA_MARKER in (ev.reasoning or ""):
                continue
            eval_changed = True
            ev.skipped = True
            ev.matched = False
            ev.verdict = ""
            ev.skip_reason = ("not-applicable: process action — " + _NA_MARKER)[:255]
            ev.reasoning = _NA_MESSAGE
            to_update.append(ev)
        if to_update and not dry:
            RuleEvaluation.objects.bulk_update(
                to_update,
                ["skipped", "matched", "verdict", "skip_reason", "reasoning"],
            )

        # 2) Recompute the run verdict from the surviving (non-skipped) evals.
        # We only ever skip NON-adverse rows, so this can never introduce a
        # defect — a clean claim stays clean.
        adverse = [
            ev for ev in evals
            if ev.matched
            and not ev.skipped
            and (ev.decision_type or "").upper() in _ADVERSE
            and (ev.decision_type or "").upper() != "BYPASS"
        ]
        if adverse:
            def _rank(ev):
                dt = (ev.decision_type or "").upper()
                return _PRECEDENCE.index(dt) if dt in _PRECEDENCE else 99
            winner = sorted(adverse, key=_rank)[0]
            final = (winner.decision_type or "DENY").upper()
        else:
            final = "ALLOW"
        verdict_changed = run.final_decision_type != final
        if not dry and verdict_changed:
            run.final_decision_type = final
            run.save(update_fields=["final_decision_type"])
        run.final_decision_type = final

        # 3) Patch the stored trace: flip the process-action entries to Skipped
        # (NOT_APPLICABLE per normalizeAuditStatus). We match on the same
        # action detector so only the (F3) step is touched.
        tchanged = False
        ct = ClaimTrace.objects.filter(run=run).first()
        if ct and isinstance(ct.trace_json, list):
            for entry in ct.trace_json:
                act = f"{entry.get('sop_action', '')}"
                if not _is_process_action(act, entry.get("decision_type", "")):
                    continue
                proc_shape_ids.add(str(entry.get("shape_id") or ""))
                already = (
                    entry.get("status") == trace_builder.SKIPPED_RULE
                    and _NA_MARKER in str(entry.get("rationale") or "")
                )
                if already:
                    continue
                entry["status"] = trace_builder.SKIPPED_RULE
                entry["step_exec_status"] = "skipped"
                entry["rationale"] = _NA_MESSAGE
                for sr in entry.get("subrule_results") or []:
                    sr["status"] = trace_builder.SKIPPED_RULE
                    if not sr.get("statement"):
                        sr["statement"] = _NA_MESSAGE
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

        # 4) Update the executive summary step line IN PLACE (NO regen, NO LLM).
        # Match by the process-action step's shape_id (fallback: action text).
        if changed and not opts.skip_exec_summary and not dry:
            es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
            if es is not None:
                fields: list[str] = []
                steps = list(es.step_summaries or [])
                sfixed = False
                for st in steps:
                    if not isinstance(st, dict):
                        continue
                    sid = str(st.get("shape_id") or "")
                    hay = f"{st.get('summary', '')}"
                    hit = (sid and sid in proc_shape_ids) or (
                        bool(_FKEY.search(hay))
                        and (bool(_CMD.search(hay)) or _is_bare_keystroke(hay))
                    )
                    if not hit:
                        continue
                    if st.get("status") != "NOT_APPLICABLE" or st.get("summary") != _NA_MESSAGE:
                        st["status"] = "NOT_APPLICABLE"
                        st["summary"] = _NA_MESSAGE
                        sfixed = True
                if sfixed:
                    es.step_summaries = steps
                    fields.append("step_summaries")
                if es.verdict != final:
                    es.verdict = final
                    fields.append("verdict")
                if fields:
                    if es.generated_by != "backfill":
                        es.generated_by = "backfill"
                        fields.append("generated_by")
                    fields.append("updated_at")
                    es.save(update_fields=fields)

        if not changed:
            return False, "already Not Applicable (idempotent)"
        return True, f"{len(proc_evals)} process-action step(s) -> N/A; verdict {final}"

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
    _p(f"  skipped  = {skipped}  (no process step / already N/A)")
    _p(f"  failed   = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
