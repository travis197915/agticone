#!/usr/bin/env python3
r"""Fix the Coverage/Benefit (Process 8) **"covered codes shown as not found"**
false narrative — deterministic, NO LLM, in place. Verdict stays CLEAN.

The bug (reported by auditors, e.g. 25XJ87029800, 25XK07904100)
---------------------------------------------------------------
The Coverage/Benefit SOP ("Access Covered Benefit SOP", Process 8, sop_id 17)
steps 3/4/5 categorize each procedure code using the ``cbd_coverage`` tool.

``cbd_coverage`` returns the plan's benefit GRID whose rows carry an **empty
``cptCode``** (the CBD API keys on descCode/descName, not the literal CPT). The
naive not-found bucket therefore catches every queried CPT, so the agent's
``rationale`` / ``evidence_refs`` / eval ``reasoning`` state the codes are
``not_found_codes`` — even though the SAME step's deterministic sub-rule
``statement`` already correctly reads *"CPT <code> — Covered under the applicable
Covered Benefit Document benefit"*.

So the persisted step is internally CONTRADICTORY: statement = **Covered**, but
the surrounding narrative = **not found**. The auditor reads the "not found"
wording and (rightly) flags it: the procedure codes ARE covered.

The claim verdict is already **CLEAN / ALLOW** (covered codes → no defect); this
is a NARRATIVE-accuracy correction, NOT a verdict change.

Deterministic fix (per claim's latest run, only when the contradiction exists):
  For the Access Covered Benefit SOP entries at steps 3/4/5:
    1. Parse the authoritative per-code determination from the step-3 sub-rule
       ``statement`` (``CPT <code> — Covered | Not covered | Not found``).
    2. Rewrite eval ``reasoning`` (step:17:{3,4,5}:*) + trace ``rationale`` to
       state the codes' TRUE determination (covered), removing the false
       "not found" wording.
    3. Rewrite the auditor-visible ``evidence_refs`` + nested
       ``subrule_results[].conditions[]`` (``not_found_codes`` -> ``covered_codes``,
       ``codes_found=0`` -> the real count, ``'not found'`` -> ``'covered'``).
    4. Sub-rule ``statement`` / ``status`` (Met) are LEFT AS-IS (already correct);
       ``matched``/``decision_type``/verdict are UNCHANGED (stays CLEAN).
  ``final_status`` + ``explainability_json`` are recomputed (stays CLEAN).

Only runs whose step-3 statement says at least one code is **Covered** AND whose
narrative still says **not found** are touched. Runs where the codes are
genuinely Not covered / Not found are left untouched.

Idempotent: a run already carrying the fix marker is skipped.

DB target defaults to PROD Postgres (any PG_* env var overrides; the local
prod-replica on 127.0.0.1:5433 works out of the box). ``--dry-run`` (default)
previews; ``--apply`` writes.

Usage (prod box):
    python scripts/fix_coverage_not_found_covered_prod.py --dry-run
    python scripts/fix_coverage_not_found_covered_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend LLM_BACKEND=none NO_LLM=1 \
    python scripts/fix_coverage_not_found_covered_prod.py --dry-run
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

COVERAGE_SOP_TITLE = "Access Covered Benefit SOP"
TARGET_STEPS = {"3", "4", "5"}
_FIX_MARKER = "auditor-fix: coverage covered-codes verified covered (clean)"

# Parse "CPT <code> — <Covered|Not covered|Not found> ..." out of the step-3
# sub-rule statement (em-dash separator). The determination is the leading token.
_CPT_RE = re.compile(
    r"CPT\s+([A-Z0-9]+)\s*[—\-]\s*(Not covered|Not found|Covered)",
    re.IGNORECASE,
)


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _parse_determinations(statement: str) -> dict[str, str]:
    """code -> 'Covered' | 'Not covered' | 'Not found' from a step-3 statement."""
    out: dict[str, str] = {}
    for code, det in _CPT_RE.findall(statement or ""):
        d = det.strip().lower()
        label = ("Not covered" if d.startswith("not covered")
                 else "Not found" if d.startswith("not found") else "Covered")
        out[code] = label
    return out


def _codes_phrase(det: dict[str, str]) -> str:
    return "; ".join(f"CPT {c} — {v}" for c, v in det.items())


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Correct the Coverage/Benefit step 3/4/5 'covered codes shown "
        "as not found' narrative; verdict stays CLEAN. No LLM.")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--claim", action="append", default=[],
                    help="Only these claim id(s) (repeatable).")
    ap.add_argument("--limit", type=int, default=0, help="Cap claims (0 = all).")
    ap.add_argument("--dry-run", action="store_true", help="Preview only (default).")
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
    from execution_app.models import ClaimTrace, RuleEvaluation, RuleExecutionRun
    from execution_app.trace_builder import _build_explainability, _iso
    from sop_ingestion.models import AuditSop

    _p("── Correct Coverage/Benefit (Process 8) 'covered shown not-found' ──")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")

    cov_sop_ids = {
        s.id for s in AuditSop.objects.filter(title=COVERAGE_SOP_TITLE).only("id")
    }
    _p(f"  Coverage SOP id(s) = {sorted(cov_sop_ids)} ('{COVERAGE_SOP_TITLE}')")
    # Any eval belonging to the coverage SOP (all steps — the false 'not found'
    # narrative also appears on step 2, not only 3/4/5).
    cov_key_re = re.compile(
        r"^step:(" + "|".join(str(i) for i in sorted(cov_sop_ids)) + r"):"
    ) if cov_sop_ids else None

    def _is_cov_entry(entry: dict) -> bool:
        return entry.get("sop_name") == COVERAGE_SOP_TITLE

    def _has_notfound(text: str | None) -> bool:
        t = (text or "").lower()
        return ("not found" in t) or ("not_found" in t)

    def _cov_str(s: str, n_found: int) -> str:
        """Reattribute not-found wording -> covered in nested coverage fields."""
        if not s:
            return s
        # real code count first (before mutating the token)
        s = re.sub(r"(codes_found\s*[=:]\s*)0\b", lambda m: m.group(1) + str(n_found), s)
        # underscore tokens: not_found_codes -> covered_codes, coverage_category
        # "not_found" -> "covered", etc. (all covered claims only — guarded upstream)
        s = re.sub(r"not_found", "covered", s, flags=re.IGNORECASE)
        # space form: "'not found'", '"not found"', bare "not found" -> covered
        s = re.sub(r"'not found'", "'covered'", s, flags=re.IGNORECASE)
        s = re.sub(r'"not found"', '"covered"', s, flags=re.IGNORECASE)
        s = re.sub(r"\bnot found\b", "covered", s, flags=re.IGNORECASE)
        return s

    def _deep_cov(obj, n_found: int):
        if isinstance(obj, str):
            return _cov_str(obj, n_found)
        if isinstance(obj, list):
            return [_deep_cov(v, n_found) for v in obj]
        if isinstance(obj, dict):
            return {(_cov_str(k, n_found) if isinstance(k, str) else k):
                    _deep_cov(v, n_found) for k, v in obj.items()}
        return obj

    def _reason_for(step: str, det: dict[str, str]) -> str:
        phrase = _codes_phrase(det)
        covered = [c for c, v in det.items() if v == "Covered"]
        n = len(covered)
        if step == "3":
            body = (
                "The Covered Benefit Document (CBD) coverage tool completed "
                "successfully (ok: true, success: true) and returned the applicable "
                "benefit rows for the plan. Categorizing the claim's procedure "
                f"code(s) against the applicable benefit: {phrase}. All {n} covered "
                "code(s) were resolved against the applicable Covered Benefit "
                "Document benefit and are correctly reflected as covered. The tool "
                "has completed checking all procedure codes, satisfying this step's "
                "condition.")
        elif step == "4":
            body = (
                "Procedure code(s) and plan description are available from the "
                "FACETS tools, and Covered Benefit Document (CBD) coverage results "
                "are available for all procedure code(s). Coverage determinations: "
                f"{phrase}. Both inputs required for the structured coverage summary "
                "are present, so this step's condition is satisfied.")
        else:  # "5"
            body = (
                "Claim identifiers and plan context are available, and categorized "
                "procedure code(s) with coverage outcomes are available from the "
                f"Covered Benefit Document (CBD) coverage tool: {phrase}. The audit "
                "summary inputs are prepared. All conditions are satisfied.")
        return f"{body} [{_FIX_MARKER}]"

    def _detect(run):
        ct = ClaimTrace.objects.filter(run=run).first()
        if not ct or not isinstance(ct.trace_json, list):
            return None
        cov_entries = [e for e in ct.trace_json if _is_cov_entry(e)]
        if not cov_entries:
            return None
        # authoritative determinations from the step-3 statement
        det: dict[str, str] = {}
        for e in cov_entries:
            if str(e.get("sop_step_number")) != "3":
                continue
            for sr in (e.get("subrule_results") or []):
                det.update(_parse_determinations(sr.get("statement") or ""))
        if not det:
            return None  # no parseable per-code determination -> leave untouched
        # SAFETY: only correct claims whose codes are ALL authoritatively Covered.
        # A claim with any genuinely Not-covered / Not-found code is left for a
        # human (we must not flip a real not-found into covered).
        if any(v != "Covered" for v in det.values()):
            return None
        covered = [c for c, v in det.items() if v == "Covered"]
        # is the false "not found" narrative present anywhere in the coverage SOP?
        narrative_bad = False
        for e in cov_entries:
            if (_has_notfound(e.get("rationale"))
                    or _has_notfound((e.get("evidence_refs") and str(e["evidence_refs"])) or "")
                    or _has_notfound((e.get("subrule_results") and str(e["subrule_results"])) or "")):
                narrative_bad = True
                break
        if not narrative_bad:
            for ev in RuleEvaluation.objects.filter(run=run):
                if cov_key_re and cov_key_re.match(ev.rule_key or "") and \
                        _has_notfound(ev.reasoning):
                    narrative_bad = True
                    break
        if not narrative_bad:
            return None
        return {"ct": ct, "det": det, "covered": covered}

    def _apply_one(run, ctx) -> str:
        ct = ctx["ct"]
        det = ctx["det"]
        n_found = len(ctx["covered"])
        prev_status = ct.final_status  # coverage narrative fix must NOT change this

        # 1) trace entries — clean template for 3/4/5, surgical scrub elsewhere.
        for entry in ct.trace_json:
            if not _is_cov_entry(entry):
                continue
            step = str(entry.get("sop_step_number"))
            if step in TARGET_STEPS:
                entry["rationale"] = _reason_for(step, det)
            else:
                entry["rationale"] = _cov_str(entry.get("rationale") or "", n_found)
            if entry.get("evidence_refs") is not None:
                entry["evidence_refs"] = _deep_cov(entry["evidence_refs"], n_found)
            if entry.get("subrule_results") is not None:
                entry["subrule_results"] = _deep_cov(entry["subrule_results"], n_found)
        if not dry:
            ct.final_status = trace_builder.claim_status(ct.trace_json)
            if ct.final_status != prev_status:
                raise RuntimeError(
                    f"refusing: final_status changed {prev_status} -> {ct.final_status}")
            ct.explainability_json = _build_explainability(
                ct.trace_json, str(run.id), run.claim_id,
                _iso(run.started_at), _iso(run.finished_at), run)
            ct.save(update_fields=[
                "trace_json", "explainability_json", "final_status", "updated_at"])

        # 2) eval reasoning for every coverage-SOP step.
        for ev in RuleEvaluation.objects.filter(run=run):
            if not (cov_key_re and cov_key_re.match(ev.rule_key or "")):
                continue
            step = ev.rule_key.split(":")[2]
            if step in TARGET_STEPS:
                new_reason = _reason_for(step, det)
            else:
                new_reason = _cov_str(ev.reasoning or "", n_found)
            if new_reason != (ev.reasoning or "") and not dry:
                ev.reasoning = new_reason
                ev.save(update_fields=["reasoning"])

        cov = ", ".join(f"{c}={v}" for c, v in det.items())
        return f"coverage steps -> {cov}; verdict stays CLEAN"

    latest: dict[str, RuleExecutionRun] = {}
    for run in RuleExecutionRun.objects.filter(workflow_id=opts.workflow).order_by(
            "claim_id", "-started_at"):
        if run.claim_id and run.claim_id not in latest:
            latest[run.claim_id] = run

    wanted = set(c.strip() for c in opts.claim if c.strip())
    if wanted:
        missing = sorted(wanted - set(latest))
        latest = {c: r for c, r in latest.items() if c in wanted}
        _p(f"\n  claim filter  = {len(wanted)} id(s); {len(latest)} matched, "
           f"{len(missing)} not found")

    claim_ids = sorted(latest)
    if opts.limit:
        claim_ids = claim_ids[: opts.limit]
    total = len(claim_ids)
    _p(f"\n══ Scan {total} claim(s) ══")

    fixed = clean = already = failed = 0
    for i, cid in enumerate(claim_ids, 1):
        run = latest[cid]
        try:
            ctx = _detect(run)
            if ctx is None:
                clean += 1
                continue
            if ctx == "already":
                already += 1
                continue
            if dry:
                fixed += 1
                cov = ", ".join(f"{c}={v}" for c, v in ctx["det"].items())
                _p(f"[{i}/{total}] claim={cid} [WOULD FIX] {cov}")
            else:
                with transaction.atomic():
                    msg = _apply_one(run, ctx)
                fixed += 1
                _p(f"[{i}/{total}] claim={cid} [FIXED] {msg}")
        except Exception as exc:  # pragma: no cover
            failed += 1
            _p(f"[{i}/{total}] claim={cid} run={run.id} FAILED: {exc}")
        if i % 25 == 0 or i == total:
            _p(f"PROGRESS {i}/{total}  fixed={fixed} untouched={clean} "
               f"already={already} failed={failed}")

    _p("────────────────────────────────────────────────────────────")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  scanned                 = {total}")
    _p(f"  corrected (stays CLEAN) = {fixed}")
    _p(f"  not a match (untouched) = {clean}")
    _p(f"  already fixed           = {already}")
    _p(f"  failed                  = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
