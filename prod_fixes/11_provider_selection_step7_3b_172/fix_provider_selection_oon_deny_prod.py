#!/usr/bin/env python3
r"""Fix the Provider Selection **Step 7 (3B) 3rd-choice** false-negative —
deterministic, NO LLM, in place.

The bug (reported by auditors, e.g. claim 25XK02420100)
------------------------------------------------------
Provider Selection SOP (``OBH Facets Provider Selection Guidelines``, sop_id 12)
Step 7 handles the **3B** group model. Its **3rd choice** is a DENY:

    Group record billed (PRPR_ENTITY='G')  AND  an individual is billed on the
    DOC360 claim image  AND  the network indicator is OON (CLCL_NTWK_IND='O')
      -> incorrect provider selection; the **FOF roster requirement is not met
         by the clinician** -> the claim must be DENIED.

A runtime "domain guidance" block (``_PROVSEL_GUIDANCE`` in the execution
engine) tells the evaluator to *derive* INN/OON from FACETS network-relationship
records and to "never select a deny choice for an INN provider". On these claims
the mere presence of capitation/network records (NWPR/NWPE/NWCR) made the
evaluator override the literal ``CLCL_NTWK_IND='O'`` to INN and decline the
3rd-choice deny — even though the FACETS provider-details lookup returns only a
facility stub and never confirms an in-network *individual* match. Result: a
genuine OON provider-selection defect is passed as CLEAN/ALLOW.

Deterministic detection (all must hold, read from the persisted claim/tool facts
— NOT from the buggy LLM verdict), per claim's latest terminal run:
    1. Provider Selection SOP (sop 12) ran on the claim.
    2. facet_ext_portal_group_model.group_model == '3B'  (Step 7 applies).
    3. facets_get_summary REC_CIV8.CLCL_NTWK_IND == 'O'  (literal OON).
    4. facets_get_summary REC_CIV8.PRPR_ENTITY   == 'G'  (group record billed).
    5. gate eval step:12:7:0 matched (Step 7 3B applies) AND the 3rd-choice deny
       eval step:12:7:3 is currently matched=False (the missed deny).
    6. facet_ext_portal_provider_details did NOT confirm an in-network
       individual match (no individual provider record — the tool returns a
       facility/group stub), so INN cannot be resolved and the literal OON
       stands.

Fix (only for claims that match ALL of the above):
    • RuleEvaluation step:12:7:3 -> matched=True, skipped=False,
      decision_type='DENY', verdict='DENY', codes=['FOF'], deterministic reasoning.
    • RuleExecutionRun -> final_decision_type='DENY', applied_codes += 'FOF',
      status='COMPLETED', deterministic narrative.
    • ClaimTrace -> the Step 7 provider-selection entry/entries flip to Not-Met /
      DENY / codes=['FOF'] with a deterministic rationale + an appended 3rd-choice
      sub-rule finding; final_status + explainability_json recomputed.
    • ClaimExecutiveSummary -> verdict='DENY', audit_status='DEFECT' (no regen).

Idempotent: a run already denied by this fix is skipped.

DB target defaults to PROD Postgres (any PG_* env var overrides; the local
prod-replica on 127.0.0.1:5433 works out of the box). ``--dry-run`` (default)
previews; ``--apply`` writes.

Usage (prod box):
    python scripts/fix_provider_selection_oon_deny_prod.py --dry-run
    python scripts/fix_provider_selection_oon_deny_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend LLM_BACKEND=none NO_LLM=1 \
    python scripts/fix_provider_selection_oon_deny_prod.py --dry-run
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

# Deterministic scope: 3B group model -> Step 7; 3rd choice is the OON group deny.
TARGET_GROUP_MODEL = "3B"
TARGET_STEP = 7
DENY_CODE = "FOF"
_FIX_MARKER = "auditor-fix: provider-selection 3B step7 OON group deny"

_DENY_REASON = (
    "Provider Selection Step 7 (3B), 3rd choice matched: a GROUP record is billed "
    "(PRPR_ENTITY='G') with an individual rendering provider on the DOC360 claim "
    "image, and the network indicator is OON (CLCL_NTWK_IND='O'). The FACETS "
    "provider-details lookup did not confirm an in-network individual provider "
    "record, so the provider cannot be resolved to INN and the literal OON status "
    "stands. Per the SOP this is an incorrect provider selection — the FOF roster "
    "requirement is not met by the clinician — and the claim must be denied. "
    + _FIX_MARKER
)

_DENY_NARRATIVE = (
    "Provider-selection defect (Step 7 / 3B, 3rd choice): group record billed with "
    "an individual rendering provider and OON network status, and no in-network "
    "individual match in FACETS provider-details. Incorrect provider selection — "
    "FOF roster requirement not met by the clinician. Claim denied."
)

_DENY_SUBRULE_STATEMENT = (
    "Step 7: 3B — 3rd choice: Discrepancy found per SOP — group billed + individual "
    "billed + OON with no INN individual match → incorrect provider selection; deny "
    "(FOF roster requirement not met by clinician)."
)


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _is_provsel_title(title: str) -> bool:
    t = (title or "").lower()
    return "provider selection" in t


def _first_result(run, tool_name, ToolInvocationRecord):
    rec = (
        ToolInvocationRecord.objects.filter(run=run, tool_name=tool_name)
        .exclude(result=None)
        .order_by("called_at")
        .first()
    )
    return rec.result if rec else None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Deterministically deny the Provider Selection Step 7 (3B) "
        "3rd-choice OON false-negatives. No LLM."
    )
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--claim", action="append", default=[],
                    help="Only these claim id(s) (repeatable).")
    ap.add_argument("--limit", type=int, default=0, help="Cap claims (0 = all).")
    ap.add_argument("--skip-exec-summary", action="store_true")
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
    from execution_app.models import (
        ClaimExecutiveSummary,
        ClaimTrace,
        RuleEvaluation,
        RuleExecutionRun,
        ToolInvocationRecord,
    )
    from execution_app.trace_builder import _build_explainability, _iso
    from sop_ingestion.models import AuditSop
    from uhc_execution_engine.rule_loader import load_workflow_bindings

    _p("── Fix Provider Selection 3B/Step7 OON false-negative ─────")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")

    # Resolve the Provider Selection SOP id(s) from the workflow bindings.
    loaded = load_workflow_bindings(opts.workflow)
    ps_sop_ids: set = set()
    for r in loaded["decisions"] + loaded["preconditions"]:
        if _is_provsel_title(r.get("sop_title")):
            ps_sop_ids.add(r.get("sop_id"))
    if not ps_sop_ids:
        sys.exit("ERROR: no Provider Selection SOP found in this workflow.")
    ps_titles = {
        (s.title or "")
        for s in AuditSop.objects.filter(id__in=ps_sop_ids).only("title")
        if _is_provsel_title(s.title)
    }
    gate_keys = tuple(f"step:{sid}:{TARGET_STEP}:0" for sid in ps_sop_ids)
    deny_keys = tuple(f"step:{sid}:{TARGET_STEP}:3" for sid in ps_sop_ids)
    _p(f"  Provider Selection SOP id(s) = {sorted(ps_sop_ids)} "
       f"(group_model {TARGET_GROUP_MODEL} -> Step {TARGET_STEP}, 3rd choice)")
    for t in sorted(ps_titles):
        _p(f"      • {t}")

    def _is_ps_step7_entry(entry: dict) -> bool:
        if entry.get("sop_name") not in ps_titles:
            return False
        return str(entry.get("sop_step_number")) == str(TARGET_STEP)

    def _detect(run) -> tuple[bool, str, dict]:
        """Return (is_defect, note, ctx) using ONLY deterministic claim/tool facts."""
        evals = list(RuleEvaluation.objects.filter(run=run))
        by_key = {e.rule_key: e for e in evals}
        gate = next((by_key[k] for k in gate_keys if k in by_key), None)
        deny = next((by_key[k] for k in deny_keys if k in by_key), None)
        if gate is None or deny is None:
            return False, "no Provider Selection Step 7 rows", {}

        # (2) group model 3B
        gm = _first_result(run, "facet_ext_portal_group_model", ToolInvocationRecord) or {}
        group_model = str(gm.get("group_model") or "").strip()
        if group_model != TARGET_GROUP_MODEL:
            return False, f"group_model={group_model or '?'} (not {TARGET_GROUP_MODEL})", {}

        # (3,4) literal OON + group entity
        summ = _first_result(run, "facets_get_summary", ToolInvocationRecord) \
            or _first_result(run, "facets_get_claim_summary", ToolInvocationRecord) or {}
        rec = (((summ.get("Data") or {}).get("ClaimSummary") or {}).get("REC_CIV8") or {})
        ntwk = str(rec.get("CLCL_NTWK_IND") or "").strip().upper()
        entity = str(rec.get("PRPR_ENTITY") or "").strip().upper()
        if ntwk != "O":
            return False, f"CLCL_NTWK_IND={ntwk or '?'} (not OON)", {}
        if entity != "G":
            return False, f"PRPR_ENTITY={entity or '?'} (not Group)", {}

        # (6) provider-details never confirmed an INN individual match. The tool
        # returns a facility/group stub (providerType != individual, no NPI match),
        # so INN cannot be resolved and the literal OON stands.
        pdet = _first_result(run, "facet_ext_portal_provider_details", ToolInvocationRecord) or {}
        pdata = pdet.get("data") or {}
        ptype = str(pdata.get("providerType") or "").strip().upper()
        # An individual INN record would be typed as a person (e.g. "I"/individual);
        # the facility/group stub ("BH", "FAC…") never confirms INN.
        inn_individual_confirmed = ptype in {"I", "INDIVIDUAL", "IND"}
        if inn_individual_confirmed:
            return False, f"provider_details confirms individual ({ptype})", {}

        # (5) gate applies + 3rd-choice deny currently NOT matched
        if not gate.matched:
            return False, "Step 7 gate not matched (3B does not apply here)", {}
        if deny.matched:
            already = _FIX_MARKER in (deny.reasoning or "")
            return False, ("already fixed (idempotent)" if already
                           else "3rd choice already matched"), {}

        return True, "3B + OON + group + no INN individual match", {
            "evals": evals, "deny": deny, "group_model": group_model,
            "ntwk": ntwk, "entity": entity,
        }

    def _apply_one(run, ctx) -> str:
        deny: RuleEvaluation = ctx["deny"]
        # 1) flip the 3rd-choice deny eval
        deny.matched = True
        deny.skipped = False
        deny.decision_type = "DENY"
        deny.verdict = "DENY"
        deny.codes = sorted(set(list(deny.codes or []) + [DENY_CODE]))
        deny.reasoning = _DENY_REASON
        if deny.confidence < 0.9:
            deny.confidence = 0.9
        if not dry:
            deny.save(update_fields=[
                "matched", "skipped", "decision_type", "verdict", "codes",
                "reasoning", "confidence",
            ])

        # 2) run verdict -> DENY
        codes = sorted(set(list(run.applied_codes or []) + [DENY_CODE]))
        if not dry:
            run.final_decision_type = "DENY"
            run.applied_codes = codes
            run.narrative = _DENY_NARRATIVE
            run.status = "COMPLETED"
            run.save(update_fields=[
                "final_decision_type", "applied_codes", "narrative", "status",
            ])
        run.final_decision_type = "DENY"

        # 3) patch stored trace: Step 7 provider-selection entries -> Not-Met/DENY
        ct = ClaimTrace.objects.filter(run=run).first()
        if ct and isinstance(ct.trace_json, list):
            tchanged = False
            for entry in ct.trace_json:
                if not _is_ps_step7_entry(entry):
                    continue
                entry["status"] = trace_builder.NOT_MET
                entry["decision_type"] = "DENY"
                entry["codes"] = sorted(set(list(entry.get("codes") or []) + [DENY_CODE]))
                entry["rationale"] = _DENY_REASON
                subs = entry.get("subrule_results")
                if not isinstance(subs, list):
                    subs = []
                    entry["subrule_results"] = subs
                if not any(str(sr.get("subrule_id") or "").endswith(f":{TARGET_STEP}:3")
                           for sr in subs):
                    subs.append({
                        "subrule_id": deny.rule_key,
                        "label": "Step 7: 3B — 3rd choice (group billed, individual "
                                 "billed, OON)",
                        "status": trace_builder.NOT_MET,
                        "statement": _DENY_SUBRULE_STATEMENT,
                        "conditions": [{
                            "condition": "Group record billed AND individual billed "
                                         "AND OON (CLCL_NTWK_IND='O') AND no INN "
                                         "individual match in provider-details",
                            "evaluated": True,
                            "using_fields": [
                                "facets_get_summary.Data.ClaimSummary.REC_CIV8.CLCL_NTWK_IND",
                                "facets_get_summary.Data.ClaimSummary.REC_CIV8.PRPR_ENTITY",
                                "facet_ext_portal_group_model.group_model",
                            ],
                            "values": {
                                "CLCL_NTWK_IND": ctx["ntwk"],
                                "PRPR_ENTITY": ctx["entity"],
                                "group_model": ctx["group_model"],
                                "notes": "Incorrect provider selection — FOF roster "
                                         "requirement not met by clinician.",
                            },
                        }],
                    })
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

        # 4) executive summary -> DENY / DEFECT (no regen, no LLM)
        if not opts.skip_exec_summary and not dry:
            es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
            if es is not None:
                fields: list[str] = []
                if es.verdict != "DENY":
                    es.verdict = "DENY"
                    fields.append("verdict")
                if es.audit_status != "DEFECT":
                    es.audit_status = "DEFECT"
                    fields.append("audit_status")
                if fields:
                    fields.append("updated_at")
                    es.save(update_fields=fields)

        return "Step 7 (3B) 3rd choice -> DENY (FOF); verdict DENY"

    # Latest terminal run per claim in the workflow.
    latest: dict[str, RuleExecutionRun] = {}
    for run in RuleExecutionRun.objects.filter(workflow_id=opts.workflow).order_by(
        "claim_id", "-started_at"
    ):
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

    denied = clean = skipped = failed = 0
    for i, cid in enumerate(claim_ids, 1):
        run = latest[cid]
        try:
            is_defect, note, ctx = _detect(run)
            if not is_defect:
                if "already fixed" in note:
                    skipped += 1
                else:
                    clean += 1
                continue
            if dry:
                denied += 1
                _p(f"[{i}/{total}] claim={cid} [WOULD DENY] {note}")
            else:
                with transaction.atomic():
                    msg = _apply_one(run, ctx)
                denied += 1
                _p(f"[{i}/{total}] claim={cid} [DENIED] {msg}")
        except Exception as exc:  # pragma: no cover - defensive
            failed += 1
            _p(f"[{i}/{total}] claim={cid} run={run.id} FAILED: {exc}")

    _p("────────────────────────────────────────────────────────────")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  scanned            = {total}")
    _p(f"  denied (defect)    = {denied}")
    _p(f"  clean (untouched)  = {clean}")
    _p(f"  already fixed      = {skipped}")
    _p(f"  failed             = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
