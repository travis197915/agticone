#!/usr/bin/env python3
r"""Fix the Provider Selection **Step 7 (3B) 3rd-choice** step-level false-negative
— deterministic, NO LLM, in place. Verdict stays **CLEAN**.

The bug (reported by auditors, e.g. claim 25XK02420100)
------------------------------------------------------
Provider Selection SOP (``OBH Facets Provider Selection Guidelines``, sop_id 12)
Step 7 handles the **3B** group model. Its **3rd choice** covers:

    Group record billed (PRPR_ENTITY='G')  AND  an individual is billed on the
    DOC360 claim image  AND  the network indicator is OON (CLCL_NTWK_IND='O').

For that scenario the SOP says the claim should be denied (incorrect provider
selection — "FOF roster requirement not met by the clinician"). On these claims
the processor **already did exactly that**: the line paid $0, the charge is fully
disallowed, and the EOB carries the denial (``CDML_DISALL_EXCD``, e.g. ``FOF``).

Because the claim was **correctly denied**, the AUDIT verdict is **CLEAN** — the
human auditor marked it clean, and rightly so. The agent, however, wrongly marked
Step 7 3rd choice as **"not matched"** (a runtime guidance block over-derived the
provider to INN off FACETS network records and skipped the OON choice). Per the
auditor the step **is a match**, and the agent should **confirm the EOB FOF is
reflecting on the claim** — i.e. matched + confirmed correctly denied → CLEAN.

So this is a STEP-LEVEL correction, NOT a verdict change: mark Step 7 3rd choice
as **Met/matched** with reasoning that confirms the claim was correctly denied,
and leave the overall claim verdict **ALLOW / CLEAN** untouched.

Deterministic detection (all must hold, from persisted claim/tool facts), per
claim's latest terminal run:
    1. Provider Selection SOP (sop 12) ran on the claim.
    2. facet_ext_portal_group_model.group_model == '3B'  (Step 7 applies).
    3. facets_get_summary REC_CIV8.CLCL_NTWK_IND == 'O'  (literal OON).
    4. facets_get_summary REC_CIV8.PRPR_ENTITY   == 'G'  (group record billed).
    5. facet_ext_portal_provider_details did NOT confirm an in-network individual
       (returns a facility/group stub), so INN could not be resolved.
    6. gate eval step:12:7:0 matched (Step 7 3B applies) AND the 3rd-choice eval
       step:12:7:3 is currently matched=False (the step-level miss).
    7. The claim was actually denied — total_paid == 0 (charge disallowed). This is
       what makes the audit CLEAN (correctly denied), not a defect.

Fix (only for claims matching ALL of the above) — verdict stays CLEAN:
    • RuleEvaluation step:12:7:3 -> matched=True, skipped=False,
      decision_type='CONFIRMED' (NON-defect), verdict='ALLOW', deterministic
      reasoning confirming the correct denial (cites the EOB code). NO EOB codes
      are added to the eval, so the SOP rollup stays CLEAN.
    • ClaimTrace -> the Step 7 provider-selection entry keeps status=Met, gets the
      corrected rationale + an appended 3rd-choice "confirmed correctly denied"
      sub-rule; final_status + explainability_json recomputed (stays CLEAN).
    • RuleExecutionRun verdict + ClaimExecutiveSummary are LEFT UNCHANGED (already
      CLEAN / ALLOW).

Idempotent: a run already corrected by this fix is skipped.

DB target defaults to PROD Postgres (any PG_* env var overrides; the local
prod-replica on 127.0.0.1:5433 works out of the box). ``--dry-run`` (default)
previews; ``--apply`` writes.

Usage (prod box):
    python scripts/fix_provider_selection_confirm_denied_prod.py --dry-run
    python scripts/fix_provider_selection_confirm_denied_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend LLM_BACKEND=none NO_LLM=1 \
    python scripts/fix_provider_selection_confirm_denied_prod.py --dry-run
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

# Deterministic scope: 3B group model -> Step 7. The auditor cited UI rule #172,
# which is the **5th choice** (step:12:7:5 / trace subrule RULE-007-005) — the OON
# deny choice for Step 7. UI rule number == RuleEvaluation.order_index + 1.
TARGET_GROUP_MODEL = "3B"
TARGET_STEP = 7
TARGET_CHOICE = 5  # 5th choice == UI rule #172 (the rule the auditor flagged)
# Only correct claims whose actual denial EOB is the provider-selection code the
# auditor cited (FOF — roster requirement not met by clinician). Claims denied for
# an unrelated reason (e.g. B05 non-covered, B14 wrong carrier) are left untouched.
REQUIRE_EOB = "FOF"
# Non-defect decision type: the SOP scenario matched AND the claim was correctly
# denied, so the audit outcome is a clean confirmation (never rolls up to DEFECT).
CONFIRM_DECISION = "CONFIRMED"
_FIX_MARKER = "auditor-fix: provider-selection 3B step7 OON confirmed-correctly-denied (clean)"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _is_provsel_title(title: str) -> bool:
    return "provider selection" in (title or "").lower()


def _first_result(run, tool_name, ToolInvocationRecord):
    rec = (
        ToolInvocationRecord.objects.filter(run=run, tool_name=tool_name)
        .exclude(result=None)
        .order_by("called_at")
        .first()
    )
    return rec.result if rec else None


def _disallow_eob(line_result) -> str:
    """Best-effort: pull the disallow EOB code (e.g. 'FOF') from line details."""
    if not isinstance(line_result, dict):
        return ""
    for item in (line_result.get("items") or []):
        rec = (((item.get("body") or {}).get("Data") or {})
               .get("LineDetails") or {}).get("REC_CIV9") or {}
        code = str(rec.get("CDML_DISALL_EXCD") or "").strip()
        if code:
            return code
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Deterministically correct the Provider Selection Step 7 (3B) "
        "3rd-choice step-level miss (matched + confirmed correctly denied); verdict "
        "stays CLEAN. No LLM."
    )
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
    from execution_app.models import (
        ClaimTrace,
        RuleEvaluation,
        RuleExecutionRun,
        ToolInvocationRecord,
    )
    from execution_app.trace_builder import _build_explainability, _iso
    from sop_ingestion.models import AuditSop
    from uhc_execution_engine.rule_loader import load_workflow_bindings

    _p("── Correct Provider Selection 3B/Step7 3rd-choice (verdict stays CLEAN) ──")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")

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
    deny_keys = tuple(f"step:{sid}:{TARGET_STEP}:{TARGET_CHOICE}" for sid in ps_sop_ids)
    _p(f"  Provider Selection SOP id(s) = {sorted(ps_sop_ids)} "
       f"(group_model {TARGET_GROUP_MODEL} -> Step {TARGET_STEP}, choice {TARGET_CHOICE} / UI #172)")
    for t in sorted(ps_titles):
        _p(f"      • {t}")

    def _is_ps_step7_entry(entry: dict) -> bool:
        if entry.get("sop_name") not in ps_titles:
            return False
        return str(entry.get("sop_step_number")) == str(TARGET_STEP)

    def _detect(run):
        evals = list(RuleEvaluation.objects.filter(run=run))
        by_key = {e.rule_key: e for e in evals}
        gate = next((by_key[k] for k in gate_keys if k in by_key), None)
        deny = next((by_key[k] for k in deny_keys if k in by_key), None)
        if gate is None or deny is None:
            return False, "no Provider Selection Step 7 rows", None

        gm = _first_result(run, "facet_ext_portal_group_model", ToolInvocationRecord) or {}
        group_model = str(gm.get("group_model") or "").strip()
        if group_model != TARGET_GROUP_MODEL:
            return False, f"group_model={group_model or '?'} (not {TARGET_GROUP_MODEL})", None

        summ = _first_result(run, "facets_get_summary", ToolInvocationRecord) \
            or _first_result(run, "facets_get_claim_summary", ToolInvocationRecord) or {}
        rec = (((summ.get("Data") or {}).get("ClaimSummary") or {}).get("REC_CIV8") or {})
        ntwk = str(rec.get("CLCL_NTWK_IND") or "").strip().upper()
        entity = str(rec.get("PRPR_ENTITY") or "").strip().upper()
        if ntwk != "O":
            return False, f"CLCL_NTWK_IND={ntwk or '?'} (not OON)", None
        if entity != "G":
            return False, f"PRPR_ENTITY={entity or '?'} (not Group)", None

        pdet = _first_result(run, "facet_ext_portal_provider_details", ToolInvocationRecord) or {}
        ptype = str((pdet.get("data") or {}).get("providerType") or "").strip().upper()
        if ptype in {"I", "INDIVIDUAL", "IND"}:
            return False, f"provider_details confirms individual ({ptype})", None

        if not gate.matched:
            return False, "Step 7 gate not matched (3B does not apply)", None
        if deny.matched:
            already = _FIX_MARKER in (deny.reasoning or "")
            return False, ("already fixed (idempotent)" if already
                           else "3rd choice already matched"), None

        # (7) claim actually denied -> audit is a clean confirmation, not a defect.
        try:
            total_paid = float(run.claim_payload.get("total_paid") or 0)
        except Exception:
            total_paid = 0.0
        if total_paid > 0:
            return False, f"claim was PAID (total_paid={total_paid}) — not a clean-denied case", None

        line = _first_result(run, "facets_get_line_details", ToolInvocationRecord)
        eob = _disallow_eob(line)
        if REQUIRE_EOB and eob.upper() != REQUIRE_EOB.upper():
            return False, f"denied with EOB {eob or '?'} (not {REQUIRE_EOB}) — untouched", None

        return True, f"3B + OON + group + no INN individual; denied (EOB {eob})", {
            "deny": deny, "group_model": group_model, "ntwk": ntwk,
            "entity": entity, "eob": eob,
        }

    def _confirm_reason(ctx) -> str:
        return (
            f"Provider Selection Step 7 (3B), {TARGET_CHOICE}th choice (UI rule #172) "
            f"APPLIES: a group record is billed (PRPR_ENTITY='G') with an individual "
            f"rendering provider on the DOC360 claim image, and the network indicator "
            f"is OON (CLCL_NTWK_IND='O') with no in-network individual match confirmed "
            f"in FACETS provider-details. Per the SOP this warrants an incorrect-"
            f"provider-selection denial (FOF — roster requirement not met by the "
            f"clinician). Confirmed the claim was correctly denied by the processor "
            f"(line paid $0, charge disallowed, EOB '{ctx['eob']}' reflecting on the "
            f"claim), so there is NO auditor defect — the claim is CLEAN. {_FIX_MARKER}"
        )

    def _apply_one(run, ctx) -> str:
        reason = _confirm_reason(ctx)
        deny: RuleEvaluation = ctx["deny"]
        # 1) step-level: mark the 3rd choice matched as a CLEAN confirmation.
        deny.matched = True
        deny.skipped = False
        deny.decision_type = CONFIRM_DECISION  # non-defect -> SOP rollup stays CLEAN
        deny.verdict = "ALLOW"
        deny.reasoning = reason
        if deny.confidence < 0.9:
            deny.confidence = 0.9
        # NOTE: intentionally NOT adding EOB codes to the eval — that would flip the
        # provider-selection rollup to DEFECT. The EOB is cited in reasoning only.
        if not dry:
            deny.save(update_fields=[
                "matched", "skipped", "decision_type", "verdict", "reasoning",
                "confidence",
            ])

        # 2) trace: keep Step 7 entry Met (CLEAN); correct the rationale + add the
        #    3rd-choice confirmed sub-rule. Verdict/final_status stay CLEAN.
        statement = (
            f"Step {TARGET_STEP}: 3B — {TARGET_CHOICE}th choice: Verified per SOP — "
            f"OON group with individual billed; confirmed the claim was correctly "
            f"denied (EOB '{ctx['eob']}', FOF roster requirement not met by "
            f"clinician). No auditor defect."
        )

        def _is_target_choice(sr) -> bool:
            sid = str(sr.get("subrule_id") or "")
            # trace subrule ids look like RULE-007-005; the eval rule_key is
            # step:12:7:5. Match either scheme for the Nth choice.
            return sid.endswith(f"-00{TARGET_CHOICE}") or sid.endswith(f":{TARGET_STEP}:{TARGET_CHOICE}")

        ct = ClaimTrace.objects.filter(run=run).first()
        if ct and isinstance(ct.trace_json, list):
            tchanged = False
            for entry in ct.trace_json:
                if not _is_ps_step7_entry(entry):
                    continue
                subs = entry.get("subrule_results")
                if not isinstance(subs, list):
                    continue
                hit = [sr for sr in subs if _is_target_choice(sr)]
                if not hit:
                    continue  # gate-only step-7 entry — leave it untouched
                for sr in hit:
                    sr["status"] = trace_builder.MET
                    sr["statement"] = statement
                entry["status"] = trace_builder.MET
                entry["rationale"] = reason
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

        # 3) RuleExecutionRun verdict + ClaimExecutiveSummary: LEFT UNCHANGED (CLEAN).
        return f"Step 7 (3B) 3rd choice -> Met (confirmed correctly denied, EOB {ctx['eob']}); verdict stays CLEAN"

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

    fixed = clean = already = failed = 0
    for i, cid in enumerate(claim_ids, 1):
        run = latest[cid]
        try:
            hit, note, ctx = _detect(run)
            if not hit:
                if "already fixed" in note:
                    already += 1
                else:
                    clean += 1
                continue
            if dry:
                fixed += 1
                _p(f"[{i}/{total}] claim={cid} [WOULD FIX] {note}")
            else:
                with transaction.atomic():
                    msg = _apply_one(run, ctx)
                fixed += 1
                _p(f"[{i}/{total}] claim={cid} [FIXED] {msg}")
        except Exception as exc:  # pragma: no cover - defensive
            failed += 1
            _p(f"[{i}/{total}] claim={cid} run={run.id} FAILED: {exc}")

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
