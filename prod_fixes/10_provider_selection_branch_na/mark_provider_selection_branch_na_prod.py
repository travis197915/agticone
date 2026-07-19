#!/usr/bin/env python3
r"""Mark NON-SELECTED Provider-Selection group-model branches as NOT APPLICABLE —
deterministically, NO LLM.

Why this exists  (tracker item #33)
-----------------------------------
The "OBH Facets Provider Selection Guidelines" SOP picks a provider record based
on the claim's **group model**, via a set of mutually-exclusive branch steps:

    Step 4 → 1A / AN / "No Group Model"
    Step 5 → 2A / 2I
    Step 6 → 3A
    Step 7 → 3B

Exactly ONE branch applies to a claim (the one matching its group model); the
others do not apply. But only the *selected* branch's sub-flow gets skipped via
``applicable_when`` — the non-selected branch's HEADER row is left as
``matched=False, skipped=False``. Because ``node_audit_status`` treats any
non-skipped row as "executed", that header rolls the whole step up to **CLEAN**
("Agent marked clean") when it should read **Not Applicable** — the auditor's
complaint on 25XJ46879400 (group model 3B → Steps 5 and 6 wrongly showed clean).

What this fixes
---------------
For each claim, a Provider-Selection branch step is "not selected" when:
  * its header row (rule_key ``step:<psel>:<n>:0``) has ``matched=False`` and is
    NOT skipped and is NON-adverse, AND
  * every other row of that step is already ``skipped`` (the branch sub-flow did
    not apply).
Those header rows are flipped to ``skipped=True`` with a NOT_APPLICABLE
skip_reason, so the step/agent rolls up to NOT APPLICABLE. The SELECTED branch
(header ``matched=True``) and any step that produced a real finding are left
untouched. Verdict is recomputed but can never regress (only non-adverse,
non-matched header rows are skipped).

Three surfaces are kept in sync, all no-LLM:
  1. ``RuleEvaluation``  — header row -> skipped / Not Applicable.
  2. ``ClaimTrace``      — the branch-header trace entry (status Met, sop_action
                           == the group-model label) -> Skipped + N/A rationale;
                           final_status + explainability recomputed.
  3. ``ClaimExecutiveSummary`` — the matching step line -> NOT_APPLICABLE (in
                           place, no regen).

Idempotent. DB target defaults to PROD Postgres (any PG_* env var overrides).
``--dry-run`` (default) previews; ``--apply`` writes.

Usage (prod box):
    python scripts/mark_provider_selection_branch_na_prod.py --dry-run
    python scripts/mark_provider_selection_branch_na_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend LLM_BACKEND=none \
    python scripts/mark_provider_selection_branch_na_prod.py --dry-run
"""
from __future__ import annotations

import argparse
import collections
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

_ADVERSE = {"DENY", "STOP", "REFER", "REFERRAL", "PEND", "PENDED", "BYPASS"}
# Adverse dispositions that constitute a real *finding* (a branch group that
# fired one of these is NEVER touched — it is a defect, not a skipped branch).
_DEFECT = {"DENY", "STOP", "REFER", "REFERRAL", "PEND", "PENDED"}
_PRECEDENCE = ["DENY", "STOP", "PEND", "PENDED", "REFER", "REFERRAL"]

# A Provider-Selection branch-header row is a group-model branch when its action
# starts with a group-model code (1A/2A/2I/3A/3B/AN/…) or says "group model".
_BRANCH_LABEL = re.compile(r"^\(?\s*(1A|1B|1I|2A|2B|2I|3A|3B|3I|AN)\b", re.I)


def _is_branch_label(action: str) -> bool:
    a = (action or "").strip()
    return bool(_BRANCH_LABEL.match(a)) or ("group model" in a.lower())

# Unique marker in the reasoning/skip_reason so re-runs are idempotent.
_NA_MARKER = "branch not selected"
_SKIP_REASON = "not-applicable: group-model " + _NA_MARKER
_NA_MESSAGE = (
    "This provider group-model branch does not match the claim's group model, "
    "so this step was not selected during provider record selection. It is "
    "neither a pass nor a defect, so it is marked Not Applicable."
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


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Mark non-selected Provider-Selection group-model branches "
        "NOT APPLICABLE; backfill existing runs. No LLM."
    )
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--claim", action="append", default=[])
    ap.add_argument("--claims-file", action="append", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-exec-summary", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
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
    from uhc_execution_engine.rule_loader import load_workflow_bindings

    # Resolve the Provider-Selection SOP id(s) from the workflow bindings.
    loaded = load_workflow_bindings(opts.workflow)
    psel_ids = {
        r.get("sop_id")
        for r in loaded["decisions"]
        if "provider selection" in (r.get("sop_title") or "").lower()
    }
    if not psel_ids:
        sys.exit("ERROR: Provider Selection SOP not found in this workflow.")
    prefixes = tuple(f"step:{sid}:" for sid in psel_ids)

    _p("── Mark non-selected Provider-Selection branches NOT APPLICABLE ──")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")
    _p(f"  Provider-Selection sop_id(s) = {sorted(psel_ids)}")

    def _collect_non_selected(evs: list) -> tuple[list, set, set]:
        """Return (rows_to_skip, step_numbers, shape_ids) for Provider-Selection
        group-model branches that were NOT selected for this claim.

        A branch is "not selected" when its header row (rule_key ``…:0``) is a
        group-model label, is NON-adverse, did NOT match (the group-model gate
        failed), and the branch produced NO defect. Every non-skipped row of
        such a branch is flipped so the whole step rolls up to NOT_APPLICABLE
        (a single lingering non-skipped child would otherwise keep it CLEAN).
        The SELECTED branch (header matched) and any branch with a real finding
        are left untouched.
        """
        groups: dict = collections.defaultdict(list)
        for e in evs:
            if not e.rule_key.startswith(prefixes):
                continue
            p = e.rule_key.split(":")
            if len(p) >= 4:
                groups[(p[1], p[2])].append(e)
        rows_to_skip: list = []
        step_numbers: set = set()
        shape_ids: set = set()
        for (_sid, step), rows in groups.items():
            header = next(
                (e for e in rows if e.rule_key.split(":")[3] == "0"), None
            )
            if not header:
                continue
            if not _is_branch_label(header.action):
                continue
            if (header.decision_type or "").upper() in _ADVERSE:
                continue
            if header.matched:  # this is the SELECTED branch — leave it
                continue
            has_defect = any(
                (not e.skipped)
                and e.matched
                and (((e.decision_type or "").upper() in _DEFECT) or e.codes)
                for e in rows
            )
            if has_defect:
                continue
            step_numbers.add(str(step))
            for e in rows:
                try:
                    if e.rule_binding_id and e.rule_binding:
                        s = getattr(e.rule_binding, "shape_id", None)
                        if s:
                            shape_ids.add(str(s))
                except Exception:
                    pass
                if not e.skipped:
                    rows_to_skip.append(e)
        return rows_to_skip, step_numbers, shape_ids

    def _seed_one(run: RuleExecutionRun) -> tuple[bool, str]:
        evals = list(RuleEvaluation.objects.filter(run=run))
        rows_to_skip, step_numbers, header_shapes = _collect_non_selected(evals)
        if not step_numbers:
            return False, "no non-selected Provider-Selection branch"

        # 1) Flip every non-skipped row of the non-selected branches to
        # skipped / Not Applicable (header + any lingering child rows).
        eval_changed = False
        to_update = []
        for ev in rows_to_skip:
            if ev.skipped and _NA_MARKER in (ev.reasoning or ""):
                continue
            eval_changed = True
            ev.skipped = True
            ev.matched = False
            ev.verdict = ""
            ev.skip_reason = _SKIP_REASON[:255]
            ev.reasoning = _NA_MESSAGE
            to_update.append(ev)
        if to_update and not dry:
            RuleEvaluation.objects.bulk_update(
                to_update,
                ["skipped", "matched", "verdict", "skip_reason", "reasoning"],
            )

        # 2) Recompute run verdict from surviving (non-skipped) evals. Headers
        # were matched=False so this never changes an existing verdict, but we
        # recompute defensively for consistency.
        adverse = [
            e for e in evals
            if e.matched and not e.skipped
            and (e.decision_type or "").upper() in _ADVERSE
            and (e.decision_type or "").upper() != "BYPASS"
        ]
        if adverse:
            def _rank(e):
                dt = (e.decision_type or "").upper()
                return _PRECEDENCE.index(dt) if dt in _PRECEDENCE else 99
            final = (sorted(adverse, key=_rank)[0].decision_type or "DENY").upper()
        else:
            final = "ALLOW"
        verdict_changed = run.final_decision_type != final
        if not dry and verdict_changed:
            run.final_decision_type = final
            run.save(update_fields=["final_decision_type"])
        run.final_decision_type = final

        # 3) Patch the trace: flip EVERY still-"Met" Provider-Selection entry
        # belonging to a non-selected branch step (matched by step number, so
        # both the branch-label entry and its sub-flow entry are covered) to
        # Skipped + N/A. Adverse entries are never touched.
        tchanged = False
        ct = ClaimTrace.objects.filter(run=run).first()
        if ct and isinstance(ct.trace_json, list):
            for entry in ct.trace_json:
                if "Provider Selection" not in (entry.get("sop_name") or ""):
                    continue
                if str(entry.get("sop_step_number")) not in step_numbers:
                    continue
                if (entry.get("status") or "") != trace_builder.MET:
                    continue
                if (entry.get("decision_type") or "").upper() in _ADVERSE:
                    continue
                entry["status"] = trace_builder.SKIPPED_RULE
                entry["step_exec_status"] = "skipped"
                entry["rationale"] = _NA_MESSAGE
                for sr in entry.get("subrule_results") or []:
                    sr["status"] = trace_builder.SKIPPED_RULE
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

        # 4) Executive summary — flip the matching step line(s) in place, no LLM.
        if changed and not opts.skip_exec_summary and not dry:
            es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
            if es is not None:
                steps = list(es.step_summaries or [])
                sfixed = False
                for st in steps:
                    if not isinstance(st, dict):
                        continue
                    sid = str(st.get("shape_id") or "")
                    if header_shapes and sid in header_shapes:
                        if st.get("status") != "NOT_APPLICABLE" or st.get("summary") != _NA_MESSAGE:
                            st["status"] = "NOT_APPLICABLE"
                            st["summary"] = _NA_MESSAGE
                            sfixed = True
                if sfixed:
                    es.step_summaries = steps
                    fields = ["step_summaries"]
                    if es.generated_by != "backfill":
                        es.generated_by = "backfill"
                        fields.append("generated_by")
                    fields.append("updated_at")
                    es.save(update_fields=fields)

        if not changed:
            return False, "already Not Applicable (idempotent)"
        return True, f"{len(step_numbers)} non-selected branch(es) -> N/A"

    # Latest terminal run per claim.
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
        except Exception as exc:  # pragma: no cover
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
    _p(f"  skipped  = {skipped}  (no non-selected branch / already N/A)")
    _p(f"  failed   = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
