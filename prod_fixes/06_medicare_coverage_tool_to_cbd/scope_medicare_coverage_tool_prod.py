#!/usr/bin/env python3
r"""Scope the Medicare coverage TOOL (``check_medicare_coverage``) to Medicare
claims only — deterministically, NO LLM.

Why this exists
---------------
The Coverage/Benefit SOP ("Access Covered Benefit SOP", Process 8) binds TWO
coverage tools on the same step:

  * ``cbd_coverage``            — the Coverage/Benefit Data tool (ALL LOBs)
  * ``check_medicare_coverage`` — a MEDICARE-ONLY coverage look-up

Because the Medicare tool carried no LOB scope it ran for EVERY claim. An
auditor flagged it firing on a Commercial claim (25XJ46879400): *"The Medicare
coverage tool was indeed called with procedure code G2074 … Claim is Commercial,
why is the SOP agent referencing a Medicare coverage tool? Possible update to
Medicare tool to CBD."*

Unlike the NPI / Provider-Opt-Out SOPs (which are wholly Medicare-only and are
skipped as a step — see ``scope_medicare_steps_prod.py``), the Coverage/Benefit
SOP is valid for every LOB. Only ONE tool inside it is Medicare-specific, so the
fix is at the TOOL level: keep the step (and ``cbd_coverage``), drop the Medicare
tool for non-Medicare claims. Every shape that binds ``check_medicare_coverage``
also binds ``cbd_coverage``, so nothing is lost.

Forward fix (already shipped in the engine)
-------------------------------------------
``rule_loader`` now tags each tool binding with an ``lob_scope`` (built-in
default ``['Medicare']`` for ``check_medicare_coverage``); ``n03_run_tools`` /
``execute_shapes`` skip invoking an out-of-LOB tool and record it under
``tools_skipped``. So every FUTURE non-Medicare run already behaves correctly
with NO LLM cost and the UI greys the tool.

This script does the two remaining, deterministic things:

  Phase A — TOOL TAGGING (forward, explicit)
      Sets ``NodeToolBinding.args_template['_lob_scope'] = ['Medicare']`` on the
      ``check_medicare_coverage`` bindings in the workflow. The engine's built-in
      default already covers this tool, but the explicit tag makes the scope
      visible in the builder and survives a tool rename.

  Phase B — BACKFILL EXISTING RUNS (claim by claim, no LLM)
      For each already-executed run whose LOB is NOT Medicare:
        1. ``ClaimTrace`` — move ``check_medicare_coverage`` from ``tools_used``
           to ``tools_skipped`` on every step, and reattribute the Medicare
           coverage references in the rationale / sub-rule text to CBD (exactly
           the auditor's "update Medicare tool to CBD"). Step verdicts are
           UNCHANGED (the coverage verdict already came via ``cbd_coverage``).
        2. ``RuleEvaluation.reasoning`` — same CBD reattribution (this is what
           the Summary tab surfaces).
        3. ``ClaimExecutiveSummary`` — sanitised IN PLACE (deterministic regex
           reattribution only). The summary is NOT regenerated — that path can
           invoke an LLM; here the verdict/status are already correct and we
           only scrub the Medicare-coverage wording to CBD. No LLM, no network.

Medicare claims are left untouched (the Medicare tool legitimately runs).

Idempotent: a run with no remaining Medicare-coverage reference is left alone.

DB target defaults to PROD Postgres (any PG_* env var overrides — the local
prod-replica works out of the box). ``--dry-run`` (default) previews; ``--apply``
writes.

Usage (prod box — bare run uses baked-in prod PG):
    python scripts/scope_medicare_coverage_tool_prod.py --dry-run
    python scripts/scope_medicare_coverage_tool_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend LLM_BACKEND=none \
    python scripts/scope_medicare_coverage_tool_prod.py --dry-run
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

MEDICARE_PRODUCT = "Medicare"
MEDICARE_TOOL = "check_medicare_coverage"
CBD_TOOL = "cbd_coverage"
TOOL_LOB_SCOPE = ["Medicare"]

# Deterministic text reattribution (auditor: "update Medicare tool to CBD").
# We only rewrite the COVERAGE-context Medicare references — i.e. "Medicare
# cover…" (coverage / coverages / coverage tool / coverage results, incl. text
# truncated mid-word) and the standalone "Medicare tool". We deliberately do
# NOT touch "Medicare-only", "non-Medicare", "Medicare opt-out", "Medicare LOB",
# "Medicare claim" or "Medicaid", which are legitimate and must stay.
_MED_TOOL_RE = re.compile(r"\bMedicare\s+tool\b", re.IGNORECASE)
_MED_COVER_RE = re.compile(r"\bMedicare(\s+cover\w*)", re.IGNORECASE)

# Auditor follow-up (Rule #218 reopen): spell out the acronym "CBD" as
# "Covered Benefit Document". Idempotent — the negative lookbehind skips any
# "CBD" that is already inside the spelled-out form "...Document (CBD)".
CBD_SPELLED = "Covered Benefit Document (CBD)"
_CBD_SPELL_RE = re.compile(r"(?<!Document \()\bCBD\b")


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _fix_text(s: str | None) -> tuple[str, bool]:
    """Reattribute Medicare-coverage references to CBD. Returns (text, changed)."""
    if not s:
        return s or "", False
    orig = s
    s = s.replace(MEDICARE_TOOL, CBD_TOOL)
    # "Medicare tool" -> "CBD coverage tool" (before the cover-word rule).
    s = _MED_TOOL_RE.sub("CBD coverage tool", s)
    # "Medicare cover…" -> "CBD cover…" (keeps the trailing cover-word intact).
    s = _MED_COVER_RE.sub(r"CBD\1", s)
    # Spell out the acronym last so both freshly-reattributed text and any
    # previously-written "CBD ..." wording get expanded to the full form.
    s = _CBD_SPELL_RE.sub(CBD_SPELLED, s)
    return s, (s != orig)


def _has_target(s: str | None) -> bool:
    if not s:
        return False
    return (
        (MEDICARE_TOOL in s)
        or bool(_MED_TOOL_RE.search(s))
        or bool(_MED_COVER_RE.search(s))
        or bool(_CBD_SPELL_RE.search(s))
    )


def _deep_fix(obj):
    """Recursively reattribute Medicare-coverage references to CBD anywhere in a
    JSON structure — string VALUES *and* dict KEYS. Returns (new_obj, changed).

    Used for the auditor-visible ``evidence_refs`` list and the nested
    ``subrule_results[].conditions[]`` blocks (condition text, notes, the
    ``using_fields`` list, and the ``values`` field-path keys), which the plain
    scalar-field fixes never reach. The tool-list fields (``tools_skipped`` etc.)
    are handled separately and are NOT passed here, so the UI still shows
    ``check_medicare_coverage`` as the skipped tool.
    """
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
            nk, ck = _fix_text(k) if isinstance(k, str) else (k, False)
            nv, cv = _deep_fix(v)
            out[nk] = nv
            changed = changed or ck or cv
        return out, changed
    return obj, False


def _deep_fix_safe(obj, skip_keys=("tool_calls",)):
    """Like ``_deep_fix`` but NEVER descends into ``skip_keys`` subtrees.

    Used for the builder JSON (``Shape.properties`` etc.) where ``tool_calls[]``
    holds the REAL bound tool identifiers (``check_medicare_coverage``). Those
    must stay verbatim — the Medicare tool legitimately runs for Medicare claims
    and the binding reference must not be rewritten to ``cbd_coverage``. Only the
    human-readable text (rule/step labels, narratives, conditions) is fixed.
    """
    if isinstance(obj, str):
        return _fix_text(obj)
    if isinstance(obj, list):
        changed = False
        out = []
        for v in obj:
            nv, c = _deep_fix_safe(v, skip_keys)
            out.append(nv)
            changed = changed or c
        return out, changed
    if isinstance(obj, dict):
        changed = False
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k in skip_keys:
                out[k] = v  # leave tool identifiers untouched
                continue
            nk, ck = _fix_text(k) if isinstance(k, str) else (k, False)
            nv, cv = _deep_fix_safe(v, skip_keys)
            out[nk] = nv
            changed = changed or ck or cv
        return out, changed
    return obj, False


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
        description="Scope the Medicare coverage tool to Medicare claims only; "
                    "backfill existing non-Medicare runs. No LLM.")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID,
                    help=f"Workflow id (default {DEFAULT_WORKFLOW_ID}).")
    ap.add_argument("--claim", action="append", default=[],
                    help="Only these claim id(s) (repeatable).")
    ap.add_argument("--claims-file", action="append", default=[],
                    help="File with claim ids (one per line / CSV first column).")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap number of claims processed (0 = all).")
    ap.add_argument("--skip-tool-tag", action="store_true",
                    help="Skip Phase A (the engine default already scopes it).")
    ap.add_argument("--skip-sop-source", action="store_true",
                    help="Skip Phase A2 (reattributing the SOP source text).")
    ap.add_argument("--skip-exec-summary", action="store_true",
                    help="Skip regenerating the executive summary in Phase B.")
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

    from agent_tools.models import NodeRuleBinding, NodeToolBinding
    from builder.models import Shape, Workbench, Workflow
    from execution_app import trace_builder
    from execution_app.models import (
        ClaimExecutiveSummary,
        ClaimTrace,
        RuleEvaluation,
        RuleExecutionRun,
    )
    from execution_app.trace_builder import _build_explainability, _iso
    from sop_ingestion.models import AuditDecision, AuditStep

    _p("── Scope the Medicare coverage TOOL to Medicare claims ─────")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")

    # ── Phase A — tag the Medicare coverage tool bindings ─────────────────────
    if not opts.skip_tool_tag:
        _p(f"\n══ Phase A — set _lob_scope={TOOL_LOB_SCOPE} on "
           f"{MEDICARE_TOOL} bindings ══")
        shape_ids = list(
            Shape.objects.filter(workbench__work_area__workflow_id=opts.workflow)
            .values_list("id", flat=True))
        tbs = NodeToolBinding.objects.filter(
            shape_id__in=shape_ids, tool__name=MEDICARE_TOOL,
        ).select_related("tool")
        touched = 0
        for tb in tbs:
            args = dict(tb.args_template or {})
            if args.get("_lob_scope") == TOOL_LOB_SCOPE:
                continue
            args["_lob_scope"] = list(TOOL_LOB_SCOPE)
            _p(f"    binding {tb.id} (shape={tb.shape_id}) _lob_scope -> "
               f"{TOOL_LOB_SCOPE!r}")
            if not dry:
                tb.args_template = args
                tb.save(update_fields=["args_template"])
            touched += 1
        _p(f"    tool bindings updated: {touched}")

    # ── Phase A2 — scrub the SOP SOURCE (AuditStep + AuditDecision) ───────────
    # The per-run trace/reasoning/exec-summary are DERIVED from the SOP source.
    # If the source still says "Medicare coverage tool", any RE-RUN regenerates
    # the wording (this is the reopen root cause). Reattribute + spell out at
    # the source so the fix is permanent. ``_fix_text`` only rewrites
    # coverage-context tokens, so Medicare opt-out / Medicaid steps are safe.
    if not opts.skip_sop_source:
        _p("\n══ Phase A2 — reattribute SOP source (AuditStep + AuditDecision) ══")
        # Scope to the SOPs actually exercised by THIS workflow (parsed from the
        # rule_key prefix "step:<sop_id>:..."), so unrelated SOPs are untouched.
        sop_ids: set[int] = set()
        for rk in (RuleEvaluation.objects
                   .filter(run__workflow_id=opts.workflow)
                   .values_list("rule_key", flat=True).distinct()):
            parts = str(rk or "").split(":")
            if len(parts) >= 2 and parts[0] == "step" and parts[1].isdigit():
                sop_ids.add(int(parts[1]))
        _p(f"    SOPs in workflow: {sorted(sop_ids)}")

        step_fields = ("question", "intro_text", "narrative_context",
                       "sub_procedure_name", "terminal_action")
        dec_fields = ("condition_if", "condition_and", "applicable_when",
                      "output_text", "action_text", "action_summary",
                      "action_line", "action_claim")

        steps_fixed = decs_fixed = 0
        step_qs = AuditStep.objects.all()
        if sop_ids:
            step_qs = step_qs.filter(sop_id__in=sop_ids)
        for st in step_qs:
            upd = []
            for f in step_fields:
                cur = getattr(st, f, None)
                if not isinstance(cur, str) or not cur:
                    continue
                new, ch = _fix_text(cur)
                if ch:
                    setattr(st, f, new)
                    upd.append(f)
            if upd:
                steps_fixed += 1
                _p(f"    AuditStep sop={st.sop_id} step={st.step_number} "
                   f"fields={'+'.join(upd)}")
                if not dry:
                    st.save(update_fields=upd)

        dec_qs = AuditDecision.objects.all()
        if sop_ids:
            dec_qs = dec_qs.filter(step__sop_id__in=sop_ids)
        for d in dec_qs.select_related("step"):
            upd = []
            for f in dec_fields:
                cur = getattr(d, f, None)
                if not isinstance(cur, str) or not cur:
                    continue
                new, ch = _fix_text(cur)
                if ch:
                    setattr(d, f, new)
                    upd.append(f)
            if upd:
                decs_fixed += 1
                if not dry:
                    d.save(update_fields=upd)
        _p(f"    AuditStep rows fixed: {steps_fixed}; "
           f"AuditDecision rows fixed: {decs_fixed}")

    # ── Phase A3 — scrub the BUILDER / WORKFLOW layer ─────────────────────────
    # This is the ACTIVE source-of-truth the dashboard renders ("Agents
    # Execution Selection" step cards) AND the copy the engine runs from on a
    # re-run. It is a separate copy from the SOP source (Phase A2), so it must
    # be reattributed too — otherwise the step cards keep showing "Medicare
    # coverage tool" even though the SOP/trace are fixed.
    if not opts.skip_sop_source:
        _p("\n══ Phase A3 — reattribute builder/workflow layer ══")

        def _fix_obj(obj):
            """Return (new, changed). JSON is fixed tool-safely (tool_calls[]
            identifiers are preserved); plain strings use _fix_text."""
            if isinstance(obj, str):
                return _fix_text(obj)
            return _deep_fix_safe(obj)

        b_counts = {"Shape": 0, "Workbench": 0, "Workflow": 0,
                    "NodeRuleBinding": 0}

        def _patch(obj, fields, key):
            upd = []
            for f in fields:
                cur = getattr(obj, f, None)
                if cur in (None, ""):
                    continue
                new, ch = _fix_obj(cur)
                if ch:
                    setattr(obj, f, new)
                    upd.append(f)
            if upd:
                b_counts[key] += 1
                if not dry:
                    obj.save(update_fields=upd)
            return upd

        for sh in Shape.objects.filter(
                workbench__work_area__workflow_id=opts.workflow):
            u = _patch(sh, ("label", "description", "properties"), "Shape")
            if u:
                _p(f"    Shape {sh.id} fields={'+'.join(u)}")
        for wb in Workbench.objects.filter(
                work_area__workflow_id=opts.workflow):
            _patch(wb, ("description", "config"), "Workbench")
        wf = Workflow.objects.filter(id=opts.workflow).first()
        if wf is not None:
            _patch(wf, ("description", "metadata"), "Workflow")
        for b in NodeRuleBinding.objects.filter(
                shape__workbench__work_area__workflow_id=opts.workflow):
            _patch(b, ("condition", "action"), "NodeRuleBinding")

        _p("    " + "; ".join(f"{k}={v}" for k, v in b_counts.items())
           + " row(s) fixed")

    # ── Phase B — backfill existing non-Medicare runs ─────────────────────────
    def _skip_reason(label: str) -> str:
        return (f"LOB {label} (non-Medicare): {MEDICARE_TOOL} applies to "
                "Medicare claims only; not invoked (coverage verified via "
                f"{CBD_TOOL})")

    def _seed_one(run: RuleExecutionRun) -> tuple[bool, str]:
        claim_lob = run.claim_lob or {}
        product = str(claim_lob.get("product") or "").strip()
        label = str(claim_lob.get("label") or product or "unknown")
        if product == MEDICARE_PRODUCT:
            return False, "Medicare claim — coverage tool legitimately runs"

        reason = _skip_reason(label)

        # 1) Patch the stored trace.
        tchanged = False
        ct = ClaimTrace.objects.filter(run=run).first()
        if ct and isinstance(ct.trace_json, list):
            for entry in ct.trace_json:
                used = list(entry.get("tools_used") or [])
                if MEDICARE_TOOL in used:
                    used = [t for t in used if t != MEDICARE_TOOL]
                    entry["tools_used"] = used
                    entry["tools_succeeded"] = [
                        t for t in (entry.get("tools_succeeded") or [])
                        if t != MEDICARE_TOOL]
                    entry["tools_failed"] = [
                        t for t in (entry.get("tools_failed") or [])
                        if t != MEDICARE_TOOL]
                    skp = list(entry.get("tools_skipped") or [])
                    if MEDICARE_TOOL not in skp:
                        skp.append(MEDICARE_TOOL)
                    entry["tools_skipped"] = skp
                    tchanged = True
                # Reattribute Medicare-coverage references -> CBD across every
                # text field the UI renders for this (non-Medicare) claim's step:
                # the rationale, the step description/action/name shown in the
                # collapsed list, and each sub-rule statement. This is the
                # claim's OWN trace snapshot, so it is LOB-appropriate to say CBD
                # (the shared SOP definition is left untouched — Medicare claims
                # still read "Medicare coverage tool").
                for fld in ("rationale", "sop_step_description", "sop_action",
                            "sop_step_name"):
                    fixed, ch = _fix_text(entry.get(fld))
                    if ch:
                        entry[fld] = fixed
                        tchanged = True
                # Deep-reattribute the auditor-visible evidence + nested rule
                # conditions (condition text, notes, using_fields, evidence_refs,
                # sub-rule statements/labels). These fields carry "Medicare
                # coverage tool … G2074 … covered=No" that the scalar fixes above
                # never reach — the exact text the reopened ticket flags. The
                # tool-list fields are intentionally excluded so the UI keeps
                # showing 'check_medicare_coverage (skipped)'.
                for fld in ("evidence_refs", "subrule_results"):
                    if entry.get(fld) is not None:
                        newv, ch = _deep_fix(entry.get(fld))
                        if ch:
                            entry[fld] = newv
                            tchanged = True
            if tchanged and not dry:
                # Verdict / status are UNCHANGED — only re-derive the cached
                # rollups so evidence refs (TOOL_OK:<name>) stay consistent.
                ct.final_status = trace_builder.claim_status(ct.trace_json)
                ct.explainability_json = _build_explainability(
                    ct.trace_json, str(run.id), run.claim_id,
                    _iso(run.started_at), _iso(run.finished_at), run)
                ct.save(update_fields=[
                    "trace_json", "explainability_json", "final_status",
                    "updated_at"])

        # 2) Patch the RuleEvaluation rows. The "Rule Evaluations" tab renders
        # the IF/THEN (condition/action) AND the agent reasoning, so all three
        # (plus skip_reason) must be reattributed — not just reasoning.
        echanged = False
        for ev in RuleEvaluation.objects.filter(run=run):
            r_fixed, rc = _fix_text(ev.reasoning)
            c_fixed, cc = _fix_text(ev.condition)
            a_fixed, ac = _fix_text(ev.action)
            s_fixed, sc = _fix_text(ev.skip_reason)
            if not (rc or cc or ac or sc):
                continue
            echanged = True
            if not dry:
                ev.reasoning = r_fixed
                ev.condition = c_fixed
                ev.action = a_fixed
                ev.skip_reason = s_fixed[:255]
                ev.save(update_fields=[
                    "reasoning", "condition", "action", "skip_reason"])

        # 3) Sanitise the EXISTING executive summary IN PLACE — deterministic
        # text reattribution only. We deliberately DO NOT regenerate the summary
        # (that path can invoke an LLM): the verdict/status are already correct,
        # we only scrub the Medicare-coverage wording to CBD. NO LLM, no network.
        eschanged = False
        if not opts.skip_exec_summary:
            es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
            if es is not None:
                h, hc = _fix_text(es.headline)
                o, oc = _fix_text(es.overall_summary)
                # Deep-fix the list fields so EVERY nested string is reattributed
                # — not just ``step_summaries[].summary``. The step entries also
                # carry an ``agent_name`` that echoes the SOP step verbiage
                # ("Step 2: Verify Medicare coverage … Use Medicare coverage
                # tool …"), which is the "Step 2 verbiage" the auditor sees.
                kf_new, kfc = _deep_fix(list(es.key_findings or []))
                steps, sc_any = _deep_fix(list(es.step_summaries or []))
                if any([hc, oc, kfc, sc_any]):
                    eschanged = True
                    if not dry:
                        es.headline = h[:512]
                        es.overall_summary = o
                        es.key_findings = kf_new
                        es.step_summaries = steps
                        es.save(update_fields=[
                            "headline", "overall_summary", "key_findings",
                            "step_summaries", "updated_at"])

        changed = tchanged or echanged or eschanged
        if not changed:
            return False, "already scoped (idempotent)"
        parts = []
        if tchanged:
            parts.append("trace")
        if echanged:
            parts.append("reasoning")
        if eschanged:
            parts.append("exec-summary")
        return True, f"{MEDICARE_TOOL} -> skipped/CBD ({label}) [{'+'.join(parts)}]"

    # Latest terminal run per claim in the workflow.
    latest: dict[str, RuleExecutionRun] = {}
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
            _p(f"PROGRESS {i}/{total} ({int(i / total * 100) if total else 100}%)  "
               f"changed={changed} skipped={skipped} failed={failed}")

    _p("────────────────────────────────────────────────────────────")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  scanned  = {total}")
    _p(f"  changed  = {changed}")
    _p(f"  skipped  = {skipped}  (Medicare / already scoped / no reference)")
    _p(f"  failed   = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
