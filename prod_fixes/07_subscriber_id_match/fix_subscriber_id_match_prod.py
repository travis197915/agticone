#!/usr/bin/env python3
r"""Mark the Subscriber-ID check MATCHED where the Doc360 image "Insured's ID
Number" is genuinely the same identity as the FACETS Subscriber ID — Physician
Claim Checklist (SOP 14, sub-rule RULE-001-001 / rule_key ``step:14:1:0``).

OFFLINE / DETERMINISTIC — NO API CALLS, NO MCP, NO LLM, NO RE-RUN.

The problem (auditor-reported, e.g. claim 25XJ57727400 — Process 1 / UI Rule #202)
-----------------------------------------------------------------------------------
The Subscriber-ID sub-rule compares the FACETS Subscriber ID against the Doc360
image "Insured's ID Number". The agent (and a prior backfill) treated any
non-equal pair as "masked / Not Applicable". That is wrong: the Doc360 image
carries the MEMBER ID, which is the FACETS **subscriber base ID (SBSB_ID) plus a
2-digit member suffix**, sometimes with a plan/product alpha prefix (e.g. ``OS``,
``STAS``, ``M2K``) or leading-zero normalization. Examples that ARE the same
identity::

    SBSB_ID K61297021  ==  Doc360 K6129702101   (base + member suffix 01)
    SBSB_ID 999095985  ==  Doc360 M2K999095985  (plan prefix + base)
    SBSB_ID C77772744  ==  Doc360 OSC7777274401 (prefix + base + suffix)
    SBSB_ID 130023933  ==  Doc360 130023933     (exact)

For 25XJ57727400 the primary SBSB_ID (155925400) does NOT contain the image ID
(N32669686). The auditor confirmed the image ID is a match on the FACETS
**Transfer Subscriber Family > Subscriber > Additional ID** screen — a field our
FACETS tools do NOT return. So it can only be corrected by auditor attestation.

What this script does (deterministic, grounded in stored data)
--------------------------------------------------------------
For every claim's LATEST run it reads:
  • SBSB_ID  — from the ``facets_get_summary`` / ``facets_get_claim_summary``
               tool result actually stored for that run.
  • Doc360 image ID — from the ClaimTrace sub-rule RULE-001-001 condition values
               (``Doc360_ID``).

It classifies the pair and only flips the check to **Met** when the match is
PROVABLE from that data:
  EXACT     — normalized image == normalized SBSB_ID.
  PROVABLE  — normalized SBSB_ID is a contiguous substring of the normalized
              image ID (base + suffix and/or prefix), or the digit cores contain
              one another (>= 6 digits).
  ATTESTED  — claim id explicitly listed in ``--attested`` (auditor verified the
              Additional-ID match by hand). Default list: 25XJ57727400.

Everything else (no provable relationship AND not attested, or no image ID
captured) is LEFT UNTOUCHED — the script never asserts a match it cannot prove.

Per matched claim it writes (into the DB the UI reads):
  1. ``RuleEvaluation`` step:14:1:0 -> matched=True, skipped=False,
     decision_type=CONDITIONAL, reasoning rewritten to the concrete match.
  2. ``ClaimTrace`` sub-rule RULE-001-001 -> status "Met" + match statement; the
     step status / final_status / explainability are recomputed.
  3. ``ClaimExecutiveSummary`` — stale masked/mismatch subscriber phrasing is
     replaced in-place with the match wording (no regeneration).

The rule is decision_type=CONDITIONAL (non-adverse), so the claim verdict
(ALLOW/DENY/PEND) is unaffected — this only corrects how the Subscriber-ID line
is presented. Idempotent: a row already Met with the match marker is skipped.
``--dry-run`` (default) previews; ``--apply`` writes.

Usage (prod box — baked-in prod PG, no network needed):
    python scripts/fix_subscriber_id_match_prod.py --dry-run
    python scripts/fix_subscriber_id_match_prod.py --apply

    # add more auditor-attested claims (repeatable):
    python scripts/fix_subscriber_id_match_prod.py --attested 25XJ57727400 \
        --attested 25XK07904100 --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend \
    python scripts/fix_subscriber_id_match_prod.py --apply
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

# Auditor-verified Additional-ID matches (Transfer Subscriber Family > Subscriber
# > Additional ID) — NOT provable from our tool payloads, corrected on attestation.
DEFAULT_ATTESTED = ["25XJ57727400"]

# Idempotency marker — unique opening of the MATCH reasoning/statement.
_MATCH_MARKER = "Subscriber ID verified — matches"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


# ── ID normalization + provable-match classification ────────────────────────
def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def _alnum(s: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (s or "")).upper()


def classify(sbsb: str | None, doc: str | None) -> str:
    """EXACT | PROVABLE | UNPROVABLE | NO_DATA — from stored data only."""
    if not sbsb or not doc:
        return "NO_DATA"
    a, b = _alnum(sbsb), _alnum(doc)
    if not a or not b:
        return "NO_DATA"
    if a == b:
        return "EXACT"
    if a in b or b in a:
        return "PROVABLE"
    da, db = _digits(sbsb), _digits(doc)
    if da and db and len(da) >= 6 and (da in db or db in da):
        return "PROVABLE"
    return "UNPROVABLE"


def _match_reasoning(sbsb: str, doc: str, cat: str) -> str:
    if cat == "ATTESTED":
        return (
            f"Subscriber ID verified — matches. The Doc360 claim image "
            f"'Insured's ID Number' ({doc}) matches the member in FACETS via "
            f"Transfer Subscriber Family > Subscriber > Additional ID "
            f"(auditor-verified). The primary Subscriber ID (SBSB_ID={sbsb}) is "
            f"the subscriber base and differs by design; the Additional ID is the "
            f"authoritative cross-reference. No discrepancy — the Subscriber ID "
            f"check is Met."
        )
    if cat == "EXACT":
        return (
            f"Subscriber ID verified — matches. FACETS Subscriber ID (SBSB_ID) "
            f"'{sbsb}' equals the Doc360 image 'Insured's ID Number' '{doc}'. "
            f"The Subscriber ID check is Met."
        )
    return (
        f"Subscriber ID verified — matches. The Doc360 image 'Insured's ID "
        f"Number' ({doc}) is the full MEMBER ID for FACETS subscriber base "
        f"SBSB_ID={sbsb} (subscriber base plus member suffix and/or plan/product "
        f"prefix). Normalized, the FACETS Subscriber ID is contained within the "
        f"image member ID, so they identify the same member. The Subscriber ID "
        f"check is Met."
    )


def _match_statement(sbsb: str, doc: str, cat: str) -> str:
    if cat == "ATTESTED":
        return (
            f"Subscriber ID: Met — Doc360 image ID {doc} matches FACETS Transfer "
            f"Subscriber Family > Subscriber > Additional ID (auditor-verified). "
            f"Primary SBSB_ID {sbsb} is the subscriber base and differs by design."
        )
    if cat == "EXACT":
        return (f"Subscriber ID: Met — FACETS SBSB_ID {sbsb} equals Doc360 image "
                f"ID {doc}.")
    return (
        f"Subscriber ID: Met — Doc360 image member ID {doc} = FACETS subscriber "
        f"base SBSB_ID {sbsb} plus member suffix/prefix (same member)."
    )


# Replace any stale subscriber-id sentence (masked/mismatch/NA/positive) with the
# match wording — the fixed claims are now confirmed matches.
_ES_MATCH_SENTENCE = "Subscriber ID matches between the Doc360 image and FACETS."
_ES_SCRUBS = (
    (re.compile(r"subscriber id[^.]*?(?:not applicable|masked|do(?:es)? not "
                r"match|mismatch|discrepan\w+|cannot be[^.]*compared|match(?:e[sd])?"
                r"|verif\w+)[^.]*\.", re.I),
     _ES_MATCH_SENTENCE),
)


def _tool_sbsb(records) -> str | None:
    for rec in records:
        r = rec.result
        if isinstance(r, str):
            try:
                r = json.loads(r)
            except Exception:
                continue
        blob = json.dumps(r, default=str)
        m = re.search(r'"SBSB_ID"\s*:\s*"?([A-Za-z0-9]+)"?', blob)
        if m:
            return m.group(1)
    return None


def _trace_doc360_id(trace_json) -> str | None:
    if not isinstance(trace_json, list):
        return None
    for n in trace_json:
        for sr in (n.get("subrule_results") or []):
            if str(sr.get("subrule_id")) == SUBSCRIBER_SUBRULE_ID:
                for c in (sr.get("conditions") or []):
                    v = c.get("values") or {}
                    if v.get("Doc360_ID"):
                        return str(v["Doc360_ID"])
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Mark Subscriber-ID Met where the Doc360 image ID provably "
                    "matches the FACETS SBSB_ID, plus auditor-attested claims "
                    "(offline, no API/MCP/LLM).")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--rule-key", default=SUBSCRIBER_RULE_KEY)
    ap.add_argument("--claim", action="append", default=[],
                    help="Restrict to these claim id(s) (repeatable).")
    ap.add_argument("--attested", action="append", default=None,
                    help="Auditor-verified Additional-ID matches (repeatable). "
                         "Defaults to 25XJ57727400 when omitted.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    opts = ap.parse_args()
    dry = not opts.apply
    attested = set(opts.attested if opts.attested is not None else DEFAULT_ATTESTED)

    for k, v in _PROD_ENV.items():
        os.environ.setdefault(k, v)
    os.environ["NO_LLM"] = "1"
    os.environ["LLM_BACKEND"] = "none"
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    import django
    django.setup()

    from django.db import transaction
    from execution_app import trace_builder
    from execution_app.models import (ClaimExecutiveSummary, ClaimTrace,
                                       RuleEvaluation, RuleExecutionRun,
                                       ToolInvocationRecord)
    from execution_app.trace_builder import _build_explainability, _iso

    _p("── Subscriber-ID → MATCHED (provable + auditor-attested) [OFFLINE] ──")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")
    _p(f"  rule_key    = {opts.rule_key}")
    _p(f"  attested    = {sorted(attested) or '(none)'}")
    _p("────────────────────────────────────────────────────────────")

    latest: dict[str, RuleExecutionRun] = {}
    q = RuleExecutionRun.objects.filter(workflow_id=opts.workflow)
    if opts.claim:
        q = q.filter(claim_id__in=opts.claim)
    for run in q.order_by("claim_id", "-started_at").only(
            "id", "claim_id", "started_at", "finished_at"):
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
        if ev.matched and not ev.skipped and _MATCH_MARKER in (ev.reasoning or ""):
            return "already Met (match)"

        ct = ClaimTrace.objects.filter(run=run).first()
        doc = _trace_doc360_id(ct.trace_json) if ct else None
        sbsb = _tool_sbsb(ToolInvocationRecord.objects.filter(
            run_id=run.id,
            tool_name__in=["facets_get_summary", "facets_get_claim_summary"]))

        cat = classify(sbsb, doc)
        if run.claim_id in attested and cat in ("UNPROVABLE", "NO_DATA"):
            cat = "ATTESTED"
        if cat not in ("EXACT", "PROVABLE", "ATTESTED"):
            return f"SKIP: {cat} (SBSB={sbsb} Doc360={doc}) — left as-is"

        reason = _match_reasoning(sbsb or "?", doc or "?", cat)
        stmt = _match_statement(sbsb or "?", doc or "?", cat)

        # 1) RuleEvaluation -> Met (matched, non-skipped).
        if not dry:
            ev.matched = True
            ev.skipped = False
            ev.skip_reason = ""
            ev.verdict = ""
            ev.decision_type = ev.decision_type or "CONDITIONAL"
            ev.reasoning = reason
            ev.save(update_fields=["matched", "skipped", "skip_reason",
                                   "verdict", "decision_type", "reasoning"])

        # 2) ClaimTrace sub-rule RULE-001-001 -> Met + match statement.
        if ct and isinstance(ct.trace_json, list):
            tchanged = False
            for entry in ct.trace_json:
                if "hysician" not in (entry.get("sop_name") or "").lower():
                    continue
                for sr in (entry.get("subrule_results") or []):
                    if str(sr.get("subrule_id")) == SUBSCRIBER_SUBRULE_ID:
                        sr["status"] = "Met"
                        sr["statement"] = stmt
                        sr["label"] = "Subscriber ID"
                        for c in (sr.get("conditions") or []):
                            c["evaluated"] = True
                        tchanged = True
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
        return f"Subscriber ID -> MET [{cat}] (SBSB={sbsb} Doc360={doc})"

    changed = already = skipped = failed = 0
    by_cat: dict[str, int] = {}
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
        if note == "already Met (match)":
            already += 1
        elif note.startswith("SKIP"):
            skipped += 1
        else:
            changed += 1
            m = re.search(r"\[(\w+)\]", note)
            if m:
                by_cat[m.group(1)] = by_cat.get(m.group(1), 0) + 1
            _p(f"[{i}/{total}] {cid} run={run.id} [CHANGED] {note}")
        if i % 25 == 0 or i == total:
            _p(f"PROGRESS {i}/{total}  changed={changed} already={already} "
               f"skipped={skipped} failed={failed}")

    _p("────────────────────────────────────────────────────────────")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  changed to MET     = {changed}  {dict(sorted(by_cat.items()))}")
    _p(f"  already MET        = {already}")
    _p(f"  left untouched     = {skipped}  (unprovable / no image id / no eval)")
    _p(f"  failed             = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
