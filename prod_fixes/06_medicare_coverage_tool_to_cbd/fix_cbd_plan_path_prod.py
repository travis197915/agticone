#!/usr/bin/env python3
r"""Fix the CBD plan/path shown on Coverage/Benefit (Process 8) — replace the
bogus ``Standard Commercial`` plan name with the claim's REAL CBD path
``<Payer> > <LOB>`` (e.g. ``Avmed > Commercial``). Deterministic, NO LLM, NO
network — every value is derived from data already stored in the prod DB.

Auditor ticket (UAT)
--------------------
    25XJ87029800 · Process 8 Coverage/Benefit · Step 2 · Rule #218
    "Agent reflects CBD for Standard Commercial was selected.
     CBD path should be Avmed > Commercial."

Root cause (evidence-based)
---------------------------
"Standard Commercial" is NOT a real CBD selection. It is a hardcoded stub the
Medicare tool (``check_medicare_coverage``) returns for non-Medicare claims::

    {"success": true, "plan_name": "Standard Commercial",
     "group_name": "Standard Commercial", "codes_found": 0,
     "coverage_details": []}

That Medicare tool was wrongly firing on Commercial claims (the bug fixed by
``scope_medicare_coverage_tool_prod.py``); the agent then surfaced its stub
"Standard Commercial" as the CBD plan name. The REAL ``cbd_coverage`` endpoint
returns generic coverage rows with NO plan/path field and ignores any
group/plan input (verified against the prod-mirror MCP server), so the correct
path has to be DERIVED from the claim's own Facets data:

    payer  <- Facets PLDS_DESC / PDDS_DESC leading token  (AVMED... -> Avmed)
             (GRGR_ID prefix used only as an unambiguous fallback, e.g. AV)
    lob    <- run.claim_lob['product']  (Commercial / Medicaid / Medicare)
    path   =  f"{payer} > {lob}"        (-> "Avmed > Commercial")

Relationship to the Medicare-scope fix
--------------------------------------
``scope_medicare_coverage_tool_prod.py`` is the FORWARD fix — it stops
``check_medicare_coverage`` (the source of the "Standard Commercial" stub) from
running on non-Medicare claims, so new runs never emit it again. Run that FIRST.
THIS script repairs the residual "Standard Commercial" text left on the
ALREADY-EXECUTED runs the auditor is re-testing in UAT.

What it patches (per claim, only where a payer is confidently derivable)
-----------------------------------------------------------------------
  * ``ClaimTrace`` — trace_json (rationale, evidence_refs, subrule_results, …
    via deep replace) + re-derived explainability_json.
  * ``RuleEvaluation`` — reasoning / condition / action.
  * ``ClaimExecutiveSummary`` — headline / overall_summary / key_findings /
    step_summaries (deep replace), IN PLACE (no regeneration, no LLM).
  * ``ToolInvocationRecord`` — the stored ``check_medicare_coverage`` result's
    ``plan_name`` / ``group_name`` so the raw tool payload no longer reads the
    stub either.

Claims whose payer cannot be confidently mapped (BH carve-outs / numeric
product codes where the leading token is not the payer) are SKIPPED and listed,
so no wrong path is ever fabricated. Extend ``PAYER_MAP`` to cover more.

Idempotent: once "Standard Commercial" is gone there is nothing to change.

DB target defaults to PROD Postgres (any PG_* env var overrides — the local
prod-replica works out of the box). ``--dry-run`` (default) previews; ``--apply``
writes.

Usage (prod box — bare run uses baked-in prod PG):
    python scripts/fix_cbd_plan_path_prod.py --dry-run
    python scripts/fix_cbd_plan_path_prod.py --apply
    python scripts/fix_cbd_plan_path_prod.py --apply --claim 25XJ87029800

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend LLM_BACKEND=none \
    python scripts/fix_cbd_plan_path_prod.py --dry-run
"""
from __future__ import annotations

import argparse
import json
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

# The hardcoded stub the Medicare tool returns for non-Medicare claims. This is
# the ONLY string we replace — matched as a whole phrase, case-insensitive.
STUB_PHRASE = "Standard Commercial"
_STUB_RE = re.compile(re.escape(STUB_PHRASE), re.IGNORECASE)

FACETS_TOOL = "facets_get_summary"
MEDICARE_TOOL = "check_medicare_coverage"

# Confident payer map, keyed on the UPPERCASED leading token of the Facets
# plan description (PLDS_DESC / PDDS_DESC). Only unambiguous, single-payer
# tokens belong here. BH carve-outs ("BH", "BH/EAP/WL", "EAP/BH") and numeric
# product codes are intentionally absent — their leading token is a product
# line, not the payer, so those claims are skipped rather than mis-labelled.
PAYER_MAP = {
    "AVMED": "Avmed",
    "MEDICA": "Medica",
    "OSCAR": "Oscar",
    "CCI": "CCI",
    "MGB": "MGB",
    "NHP": "NHP",
    "PHS": "PHS",
    "SHP": "SHP",
    "ALLWAYS": "AllWays",
    "BLUE": "Blue",
    "UHC": "UHC",
}
# Multi-word plan prefixes checked before the single-token map.
PAYER_PREFIX_MAP = (
    ("HEALTH FIRST", "Health First"),
)
# GRGR_ID 2-char prefix -> payer, used ONLY as a fallback and ONLY for prefixes
# that map to exactly one payer across the dataset (AW is shared by MGB and
# AllWays, so it is deliberately excluded).
GRGR_PREFIX_MAP = {
    "AV": "Avmed",
    "MC": "Medica",
    "OH": "Oscar",
    "CC": "CCI",
    "HF": "Health First",
    "RI": "NHP",
    "SH": "SHP",
}

VALID_LOBS = ("Commercial", "Medicaid", "Medicare")


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _facets_plan_fields(run) -> tuple[str, str]:
    """Return ``(plan_desc, grgr_id)`` from the stored facets_get_summary result."""
    rec = (run.tool_invocations
           .filter(tool_name=FACETS_TOOL).order_by("-called_at").first())
    if not rec:
        return "", ""
    blob = json.dumps(rec.result or {})
    def _first(*keys: str) -> str:
        for k in keys:
            m = re.search(rf'"{k}"\s*:\s*"([^"]+)"', blob)
            if m and m.group(1).strip():
                return m.group(1).strip()
        return ""
    plan = _first("PLDS_DESC", "PDDS_DESC")
    grgr = _first("GRGR_ID")
    return plan, grgr


def _derive_payer(plan_desc: str, grgr_id: str) -> str:
    """Best-effort payer display name; "" when not confidently derivable."""
    pd = (plan_desc or "").strip().upper()
    for prefix, name in PAYER_PREFIX_MAP:
        if pd.startswith(prefix):
            return name
    token = pd.split()[0] if pd else ""
    if token in PAYER_MAP:
        return PAYER_MAP[token]
    gp = (grgr_id or "").strip().upper()[:2]
    if gp in GRGR_PREFIX_MAP:
        return GRGR_PREFIX_MAP[gp]
    return ""


def _derive_lob(run, plan_desc: str) -> str:
    """LOB product from the engine-computed claim_lob, with a keyword fallback."""
    product = str((run.claim_lob or {}).get("product") or "").strip()
    if product in VALID_LOBS:
        return product
    pd = (plan_desc or "").upper()
    if "MEDICARE" in pd:
        return "Medicare"
    if "MEDICAID" in pd:
        return "Medicaid"
    return "Commercial"


def _make_fixers(path: str):
    """Build ``_fix_text`` / ``_deep_fix`` closures bound to this claim's path."""

    def _fix_text(s):
        if not isinstance(s, str) or not s:
            return s, False
        new = _STUB_RE.sub(path, s)
        return new, (new != s)

    def _deep_fix(obj):
        if isinstance(obj, str):
            return _fix_text(obj)
        if isinstance(obj, list):
            changed = False
            out = []
            for v in obj:
                nv, c = _deep_fix(v)
                out.append(nv)
                changed = changed or c
            return out, changed
        if isinstance(obj, dict):
            changed = False
            out = {}
            for k, v in obj.items():
                nk, ck = (_fix_text(k) if isinstance(k, str) else (k, False))
                nv, cv = _deep_fix(v)
                out[nk] = nv
                changed = changed or ck or cv
            return out, changed
        return obj, False

    return _fix_text, _deep_fix


def _load_claim_ids(paths, inline):
    ids = set(c.strip() for c in inline if c.strip())
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
        description="Replace the 'Standard Commercial' CBD stub with the "
                    "claim's real '<Payer> > <LOB>' path. No LLM, no network.")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID,
                    help=f"Workflow id (default {DEFAULT_WORKFLOW_ID}).")
    ap.add_argument("--claim", action="append", default=[],
                    help="Only these claim id(s) (repeatable).")
    ap.add_argument("--claims-file", action="append", default=[],
                    help="File with claim ids (one per line / CSV first column).")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap number of claims processed (0 = all).")
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
        ToolInvocationRecord,
    )
    from execution_app.trace_builder import _build_explainability, _iso

    _p("── Fix CBD plan/path ('Standard Commercial' -> '<Payer> > <LOB>') ──")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")

    def _fix_one(run) -> tuple[bool, str]:
        plan_desc, grgr_id = _facets_plan_fields(run)
        payer = _derive_payer(plan_desc, grgr_id)
        if not payer:
            return False, (f"payer not derivable (PLDS='{plan_desc[:40]}' "
                           f"GRGR_ID='{grgr_id}') — skipped")
        lob = _derive_lob(run, plan_desc)
        path = f"{payer} > {lob}"
        _fix_text, _deep_fix = _make_fixers(path)

        touched = []

        # 1) ClaimTrace — trace_json + explainability re-derivation.
        ct = ClaimTrace.objects.filter(run=run).first()
        if ct and isinstance(ct.trace_json, list):
            new_trace, tch = _deep_fix(ct.trace_json)
            if tch:
                ct.trace_json = new_trace
                if not dry:
                    ct.final_status = trace_builder.claim_status(ct.trace_json)
                    ct.explainability_json = _build_explainability(
                        ct.trace_json, str(run.id), run.claim_id,
                        _iso(run.started_at), _iso(run.finished_at), run)
                    ct.save(update_fields=[
                        "trace_json", "explainability_json", "final_status",
                        "updated_at"])
                touched.append("trace")

        # 2) RuleEvaluation — reasoning / condition / action.
        ev_changed = 0
        for ev in RuleEvaluation.objects.filter(run=run):
            r, rc = _fix_text(ev.reasoning)
            c, cc = _fix_text(ev.condition)
            a, ac = _fix_text(ev.action)
            if not (rc or cc or ac):
                continue
            ev_changed += 1
            if not dry:
                ev.reasoning, ev.condition, ev.action = r, c, a
                ev.save(update_fields=["reasoning", "condition", "action"])
        if ev_changed:
            touched.append(f"reasoning({ev_changed})")

        # 3) ClaimExecutiveSummary — deep, in place (no regeneration).
        es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
        if es is not None:
            h, hc = _fix_text(es.headline)
            o, oc = _fix_text(es.overall_summary)
            kf, kfc = _deep_fix(list(es.key_findings or []))
            steps, stc = _deep_fix(list(es.step_summaries or []))
            if any([hc, oc, kfc, stc]):
                if not dry:
                    es.headline = h[:512]
                    es.overall_summary = o
                    es.key_findings = kf
                    es.step_summaries = steps
                    es.save(update_fields=[
                        "headline", "overall_summary", "key_findings",
                        "step_summaries", "updated_at"])
                touched.append("exec-summary")

        # 4) ToolInvocationRecord — scrub the stub in the raw Medicare-tool result.
        tir_changed = 0
        for rec in ToolInvocationRecord.objects.filter(
                run=run, tool_name=MEDICARE_TOOL):
            new_res, rc = _deep_fix(rec.result or {})
            if rc:
                tir_changed += 1
                if not dry:
                    rec.result = new_res
                    rec.save(update_fields=["result"])
        if tir_changed:
            touched.append(f"tool-result({tir_changed})")

        if not touched:
            return False, f"no '{STUB_PHRASE}' present ({path}) — idempotent"
        return True, f"-> '{path}'  [{'+'.join(touched)}]"

    # Latest terminal run per claim in the workflow.
    latest = {}
    for run in RuleExecutionRun.objects.filter(
            workflow_id=opts.workflow).order_by("claim_id", "-started_at"):
        if run.claim_id and run.claim_id not in latest:
            latest[run.claim_id] = run

    wanted = _load_claim_ids(opts.claims_file, opts.claim)
    if wanted:
        missing = sorted(wanted - set(latest))
        latest = {c: r for c, r in latest.items() if c in wanted}
        _p(f"\n  claim filter  = {len(wanted)} id(s); {len(latest)} matched, "
           f"{len(missing)} not found")
        if missing:
            _p(f"  not found     = {', '.join(missing)}")

    claim_ids = sorted(latest)
    if opts.limit:
        claim_ids = claim_ids[: opts.limit]
    total = len(claim_ids)
    _p(f"\n══ scanning {total} run(s) ══")

    changed = idempotent = skipped_payer = failed = 0
    unmapped = []
    for i, cid in enumerate(claim_ids, 1):
        run = latest[cid]
        try:
            if dry:
                did, note = _fix_one(run)
            else:
                with transaction.atomic():
                    did, note = _fix_one(run)
        except Exception as exc:  # pragma: no cover - defensive
            failed += 1
            _p(f"[{i}/{total}] claim={cid} run={run.id} FAILED: {exc}")
            continue
        if did:
            changed += 1
            _p(f"[{i}/{total}] claim={cid} [CHANGED] {note}")
        elif "payer not derivable" in note:
            skipped_payer += 1
            unmapped.append(cid)
        else:
            idempotent += 1
        if i % 25 == 0 or i == total:
            _p(f"PROGRESS {i}/{total} ({int(i / total * 100) if total else 100}%)  "
               f"changed={changed} idempotent={idempotent} "
               f"unmapped={skipped_payer} failed={failed}")

    _p("────────────────────────────────────────────────────────────")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  scanned    = {total}")
    _p(f"  changed    = {changed}")
    _p(f"  idempotent = {idempotent}  (no '{STUB_PHRASE}' present)")
    _p(f"  unmapped   = {skipped_payer}  (payer not confidently derivable — skipped)")
    _p(f"  failed     = {failed}")
    if unmapped:
        _p("\n  Unmapped claims (extend PAYER_MAP to cover these):")
        _p("    " + ", ".join(unmapped))
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
