#!/usr/bin/env python3
r"""Fix Provider-Selection GROUP-MODEL routing rules: AND -> OR (membership) —
deterministically, NO LLM.

Why this exists  (tracker item — claim 25XJ88210600, rule #152)
---------------------------------------------------------------
"OBH Facets Provider Selection Guidelines" Step 3 routes to the right branch
based on the claim's **group model**, via a set of mutually-exclusive rows:

    Step 3 row → group model set → action
      RULE-003-001  1A, AN, or No Group Model  → Proceed to next step
      RULE-003-002  2A, 2I                      → Skip to Step 5   (rule #152)
      RULE-003-003  3A                          → Skip to Step 6
      RULE-003-004  3B                          → Skip to Step 7

Each row stores the group-model set in BOTH ``condition_if`` and
``condition_and`` (a restatement), and the engine renders the condition as
``condition_if AND condition_and`` (rule_loader._hydrate_decision, line ~156):

    "2A, 2I from facet extension portal. AND 2A, 2I"

The evaluator then reads the "AND 2A, 2I" as an ADDITIONAL requirement and
concludes the claim must be BOTH 2A *and* 2I. A group model is a single value
(e.g. "2I"), so the rule can NEVER match — even though "2A, 2I" is meant as a
membership set (match 2A OR 2I). Auditor complaint on 25XJ88210600 (group model
2I): rule #152 shows "Not Met — requires BOTH '2A' AND '2I'"; it should be
"Matched" (provider is 2I), and the remaining group-model rows Not Applicable.

The buggy "AND" text lives in the BAKED binding override
(``NodeRuleBinding.condition``) which the engine prefers over the live SOP row,
so the fix must rewrite the binding (and the ``AuditDecision`` for consistency).

What this does
--------------
Phase A — definition fix (future runs, no LLM):
  For every group-model routing row (a row whose ``condition_and`` is purely
  group-model tokens), rewrite ``NodeRuleBinding.condition`` and
  ``AuditDecision.condition_if`` to explicit OR/membership phrasing and clear
  ``AuditDecision.condition_and`` so no "AND <set>" is emitted. Non group-model
  rows (the entity-locate rows 0/1) are never touched.

Phase B — backfill existing runs (no LLM):
  For each claim, read its group model from the ``facet_ext_portal_group_model``
  tool result (authoritative), pick the routing row whose set contains it, then:
    * that row  -> matched=True  (corrected reasoning + OR condition text)
    * the others-> Not Applicable (skipped=True, matched=False)
  Three surfaces kept in sync, all no-LLM:
    1. RuleEvaluation       — routing rows re-verdicted as above.
    2. ClaimTrace           — the routing shape entry -> Met with the correct
                              action/rationale; its sub-rules re-stated.
    3. ClaimExecutiveSummary— the routing shape line corrected in place.
  Verdict is recomputed defensively (routing rows are non-adverse CONDITIONAL,
  so a verdict can never regress).

Idempotent. DB target defaults to PROD Postgres (any PG_* env var overrides).
``--dry-run`` (default) previews; ``--apply`` writes.

Usage (prod box):
    python scripts/fix_provsel_group_model_or_prod.py --dry-run
    python scripts/fix_provsel_group_model_or_prod.py --apply

Local prod-replica:
    APP_ENV=local PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres \
    PG_PASSWORD=postgres PG_DATABASE=uhc_backend LLM_BACKEND=none \
    python scripts/fix_provsel_group_model_or_prod.py --dry-run
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

# Known Facets group-model codes. "No Group Model" is handled separately.
_GM_TOKEN_RE = re.compile(r"\b(1A|1B|1I|2A|2B|2I|3A|3B|3I|AN)\b", re.I)
_NO_GM_RE = re.compile(r"no\s+group\s+model", re.I)
# Connectors / filler allowed in a "pure group-model set" condition_and.
_FILLER_RE = re.compile(r"\b(or|and|from|facet|facets|extension|portal|the|is|group|model)\b", re.I)

_ADVERSE = {"DENY", "STOP", "REFER", "REFERRAL", "PEND", "PENDED", "BYPASS"}
_PRECEDENCE = ["DENY", "STOP", "PEND", "PENDED", "REFER", "REFERRAL"]

# Idempotency markers embedded in the corrected text.
_MATCH_MARKER = "matching ANY ONE"
_NA_MARKER = "Not Applicable to this claim"
_SKIP_REASON = "not-applicable: group-model does not match this routing branch"
# When the selected routing row was skipped for one of these reasons the whole
# Provider-Selection SOP was halted upstream (out of scope / auditing stopped),
# so we must NOT fabricate a match — the step legitimately did not execute.
_HALT_MARKERS = ("out of scope", "auditing stopped", "prior step")
# Stable prefix written into ``condition_if`` by Phase A — lets the detector
# re-recognise a routing row even after it has already been rewritten (so the
# script stays fully re-runnable).
_REWRITE_MARKER = "Group model (from the Facets extension portal)"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


# ── group-model set parsing ────────────────────────────────────────────────
def _parse_gm_set(text: str) -> tuple[tuple[str, ...], bool]:
    """Return (ordered unique codes upper, includes_no_group_model)."""
    if not text:
        return (), False
    codes: list[str] = []
    for m in _GM_TOKEN_RE.finditer(text):
        c = m.group(1).upper()
        if c not in codes:
            codes.append(c)
    return tuple(codes), bool(_NO_GM_RE.search(text))


def _is_pure_gm_set(text: str) -> bool:
    """True when ``text`` is only group-model tokens + filler (no real prose).

    This is how we distinguish a routing row (condition_and == "2A, 2I") from
    the entity-locate rows whose condition_and is a long sentence.
    """
    if not text:
        return False
    codes, no_gm = _parse_gm_set(text)
    if not codes and not no_gm:
        return False
    scrubbed = _GM_TOKEN_RE.sub(" ", text)
    scrubbed = _NO_GM_RE.sub(" ", scrubbed)
    scrubbed = _FILLER_RE.sub(" ", scrubbed)
    scrubbed = re.sub(r"[\s,.\-–/()]+", " ", scrubbed).strip()
    return scrubbed == ""


def _human_set(codes: tuple[str, ...], no_gm: bool) -> str:
    parts = list(codes) + (["No Group Model"] if no_gm else [])
    return " or ".join(parts) if parts else "(unspecified)"


def _or_condition(codes: tuple[str, ...], no_gm: bool) -> str:
    human = _human_set(codes, no_gm)
    parts = list(codes) + (["No Group Model"] if no_gm else [])
    if len(parts) <= 1:
        return f"Group model (from the Facets extension portal) is {human}."
    return (
        f"Group model (from the Facets extension portal) is one of: {human}. "
        f"Match if the claim's group model equals ANY ONE of these values — "
        f"they do NOT all need to be present."
    )


def _match_msg(gm: str, codes: tuple[str, ...], no_gm: bool, action: str) -> str:
    human = _human_set(codes, no_gm)
    act = (action or "").strip()
    tail = f" Action: {act}" if act else ""
    return (
        f"The claim's group model (from the Facets extension portal) is '{gm}'. "
        f"This branch applies when the group model is {human} — {_MATCH_MARKER} of "
        f"these values is sufficient (they do not all need to be present). Since "
        f"'{gm}' is one of them, the condition is satisfied (matched).{tail}"
    )


def _na_msg(gm: str, codes: tuple[str, ...], no_gm: bool) -> str:
    human = _human_set(codes, no_gm)
    return (
        f"The claim's group model is '{gm}', which is not {human}. This "
        f"group-model routing branch is therefore {_NA_MARKER}."
    )


def _norm_gm(v) -> str:
    return str(v or "").strip().upper()


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
        description="Fix Provider-Selection group-model routing (AND->OR). No LLM."
    )
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--claim", action="append", default=[])
    ap.add_argument("--claims-file", action="append", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-definition", action="store_true",
                    help="skip Phase A (binding/AuditDecision rewrite)")
    ap.add_argument("--skip-backfill", action="store_true",
                    help="skip Phase B (existing-run backfill)")
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

    from agent_tools.models import NodeRuleBinding
    from builder.models import Shape
    from execution_app import trace_builder
    from execution_app.models import (
        ClaimExecutiveSummary,
        ClaimTrace,
        RuleEvaluation,
        RuleExecutionRun,
    )
    from execution_app.trace_builder import _build_explainability, _iso
    from sop_ingestion.models import AuditDecision, AuditStep
    from uhc_execution_engine.rule_loader import load_workflow_bindings

    # ── Resolve the Provider-Selection SOP id(s) for this workflow ──────────
    loaded = load_workflow_bindings(opts.workflow)
    psel_ids = {
        r.get("sop_id")
        for r in loaded["decisions"]
        if "provider selection" in (r.get("sop_title") or "").lower()
    }
    if not psel_ids:
        sys.exit("ERROR: Provider Selection SOP not found in this workflow.")

    # ── Build the routing-row map from the SOP: rule_key -> group-model set ──
    # A routing row is one whose ``condition_and`` is a pure group-model set.
    route_map: dict[str, dict] = {}  # rule_key -> {codes,no_gm,action,dec_id}
    for sid in psel_ids:
        for step in AuditStep.objects.filter(sop_id=sid):
            for dec in AuditDecision.objects.filter(step=step):
                cand_and = dec.condition_and or ""
                cif = dec.condition_if or ""
                is_orig = _is_pure_gm_set(cand_and)          # "2A, 2I"
                is_rewr = cif.strip().startswith(_REWRITE_MARKER)  # already fixed
                if not (is_orig or is_rewr):
                    continue
                probe = cand_and if is_orig else cif
                codes, no_gm = _parse_gm_set(probe)
                if not codes and not no_gm:
                    continue
                rk = f"step:{sid}:{step.step_number}:{dec.row_index}"
                route_map[rk] = {
                    "codes": codes,
                    "no_gm": no_gm,
                    "action": (dec.action_text or dec.action_summary or "").strip(),
                    "dec_id": dec.id,
                    "step_number": step.step_number,
                    "row_index": dec.row_index,
                }

    _p("── Fix Provider-Selection group-model routing (AND -> OR) ──")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")
    _p(f"  Provider-Selection sop_id(s) = {sorted(psel_ids)}")
    _p(f"  group-model routing rows found = {len(route_map)}")
    for rk, meta in sorted(route_map.items()):
        _p(f"    {rk}  set={_human_set(meta['codes'], meta['no_gm'])!r}  "
           f"action={meta['action'][:32]!r}")

    # ── Phase A: rewrite binding + AuditDecision condition (AND -> OR) ───────
    if not opts.skip_definition:
        _p("\n══ Phase A: definition fix (binding + AuditDecision) ══")
        shape_ids = list(
            Shape.objects.filter(
                workbench__work_area__workflow_id=opts.workflow
            ).values_list("id", flat=True)
        )
        a_bind = a_dec = a_skip = 0
        for rk, meta in sorted(route_map.items()):
            new_cond = _or_condition(meta["codes"], meta["no_gm"])
            # binding override (what the engine actually reads)
            for rb in NodeRuleBinding.objects.filter(
                shape_id__in=shape_ids, rule_key=rk
            ):
                if (rb.condition or "").strip() == new_cond:
                    a_skip += 1
                    continue
                _p(f"  [binding] {rk}: {(rb.condition or '')[:48]!r} -> OR")
                if not dry:
                    rb.condition = new_cond
                    rb.save(update_fields=["condition", "updated_at"])
                a_bind += 1
            # live SOP row (consistency)
            dec = AuditDecision.objects.filter(id=meta["dec_id"]).first()
            if dec is not None:
                if (dec.condition_if or "").strip() != new_cond or (dec.condition_and or ""):
                    if not dry:
                        dec.condition_if = new_cond
                        dec.condition_and = ""
                        dec.save(update_fields=["condition_if", "condition_and"])
                    a_dec += 1
        _p(f"  bindings updated={a_bind}  decisions updated={a_dec}  "
           f"already-clean bindings={a_skip}")

    if opts.skip_backfill:
        _p("\n(skip-backfill) done.")
        return

    # ── Phase B: backfill existing runs ─────────────────────────────────────
    from django.apps import apps
    TIR = apps.get_model("execution_app", "ToolInvocationRecord")

    def _claim_group_model(run) -> str:
        for t in TIR.objects.filter(
            run=run, tool_name="facet_ext_portal_group_model"
        ).order_by("-called_at"):
            gm = _norm_gm((t.result or {}).get("group_model"))
            if gm:
                return gm
        # Fallback: parse the group model from routing eval reasonings and take
        # the majority (deterministic — never depends on DB row ordering).
        import collections as _c
        votes: _c.Counter = _c.Counter()
        for e in RuleEvaluation.objects.filter(run=run):
            if e.rule_key not in route_map:
                continue
            m = re.search(
                r"group[_ ]model\D+?'([0-9][A-Z]|AN)'", e.reasoning or "", re.I)
            if m:
                votes[_norm_gm(m.group(1))] += 1
        if votes:
            return votes.most_common(1)[0][0]
        return ""

    def _select_row(gm: str) -> str | None:
        for rk, meta in route_map.items():
            if gm in meta["codes"]:
                return rk
            if meta["no_gm"] and gm in ("", "NONE", "NO GROUP MODEL"):
                return rk
        return None

    def _seed_one(run: RuleExecutionRun) -> tuple[bool, str]:
        evals = list(RuleEvaluation.objects.filter(run=run))
        routing_evals = [e for e in evals if e.rule_key in route_map]
        if not routing_evals:
            return False, "no group-model routing rows in run"

        gm = _claim_group_model(run)
        if not gm:
            return False, "group model unknown — skipped"
        sel = _select_row(gm)
        if sel is None:
            return False, f"group model {gm!r} not covered by any routing row"

        # Guard: if the selected routing row was skipped because the SOP halted
        # upstream (out of scope / auditing stopped), do NOT fabricate a match.
        sel_ev = next((e for e in routing_evals if e.rule_key == sel), None)
        if sel_ev is not None and sel_ev.skipped:
            blob = f"{sel_ev.skip_reason or ''} {sel_ev.reasoning or ''}".lower()
            if any(m in blob for m in _HALT_MARKERS):
                return False, f"provider selection halted upstream (gm={gm}) — left as-is"

        sel_meta = route_map[sel]

        # 1) Re-verdict routing rows.
        eval_changed = False
        routing_shapes: set = set()
        to_update = []
        for ev in routing_evals:
            try:
                if ev.rule_binding_id and ev.rule_binding:
                    s = getattr(ev.rule_binding, "shape_id", None)
                    if s:
                        routing_shapes.add(str(s))
            except Exception:
                pass
            meta = route_map[ev.rule_key]
            new_cond = _or_condition(meta["codes"], meta["no_gm"])
            if ev.rule_key == sel:
                want_reason = _match_msg(gm, meta["codes"], meta["no_gm"], meta["action"])
                if ev.matched and not ev.skipped and _MATCH_MARKER in (ev.reasoning or ""):
                    continue
                ev.matched = True
                ev.skipped = False
                ev.verdict = ""
                ev.skip_reason = ""
                ev.reasoning = want_reason
                ev.condition = new_cond
            else:
                if ev.skipped and _NA_MARKER in (ev.reasoning or ""):
                    continue
                ev.matched = False
                ev.skipped = True
                ev.verdict = ""
                ev.skip_reason = _SKIP_REASON[:255]
                ev.reasoning = _na_msg(gm, meta["codes"], meta["no_gm"])
                ev.condition = new_cond
            eval_changed = True
            to_update.append(ev)
        if to_update and not dry:
            RuleEvaluation.objects.bulk_update(
                to_update,
                ["matched", "skipped", "verdict", "skip_reason", "reasoning", "condition"],
            )

        # 2) Recompute verdict defensively (routing rows are non-adverse).
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

        # 3) Patch the routing shape trace entry.
        tchanged = False
        ct = ClaimTrace.objects.filter(run=run).first()
        if ct and isinstance(ct.trace_json, list) and routing_shapes:
            sel_action = sel_meta["action"] or "Proceed."
            sel_reason = _match_msg(gm, sel_meta["codes"], sel_meta["no_gm"], sel_meta["action"])
            # Sub-rules, one per routing row in document order.
            sub_rows = sorted(route_map.items(), key=lambda kv: (kv[1]["step_number"], kv[1]["row_index"]))
            for entry in ct.trace_json:
                if str(entry.get("shape_id")) not in routing_shapes:
                    continue
                if _MATCH_MARKER in (entry.get("rationale") or "") and entry.get("status") == trace_builder.MET:
                    continue
                entry["status"] = trace_builder.MET
                entry["step_exec_status"] = "success"
                entry["sop_action"] = sel_action
                entry["rationale"] = sel_reason
                sd = entry.get("sop_step_description")
                if isinstance(sd, str) and " AND " in sd:
                    entry["sop_step_description"] = _or_condition(
                        sel_meta["codes"], sel_meta["no_gm"])
                new_subs = []
                for rk, meta in sub_rows:
                    is_sel = rk == sel
                    new_subs.append({
                        "rule_key": rk,
                        "status": trace_builder.MET if is_sel else trace_builder.SKIPPED_RULE,
                        "action": meta["action"],
                        "condition": _or_condition(meta["codes"], meta["no_gm"]),
                        "reasoning": (
                            _match_msg(gm, meta["codes"], meta["no_gm"], meta["action"])
                            if is_sel else _na_msg(gm, meta["codes"], meta["no_gm"])
                        ),
                    })
                entry["subrule_results"] = new_subs
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

        # 4) Executive summary — fix the routing shape line in place, no LLM.
        if changed and not opts.skip_exec_summary and not dry and routing_shapes:
            es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
            if es is not None:
                steps = list(es.step_summaries or [])
                sfixed = False
                want = "CLEAN — " + _match_msg(
                    gm, sel_meta["codes"], sel_meta["no_gm"], sel_meta["action"])
                for st in steps:
                    if not isinstance(st, dict):
                        continue
                    if str(st.get("shape_id") or "") not in routing_shapes:
                        continue
                    if st.get("status") == "CLEAN" and _MATCH_MARKER in (st.get("summary") or ""):
                        continue
                    st["status"] = "CLEAN"
                    st["summary"] = want
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
            return False, "already correct (idempotent)"
        return True, f"gm={gm} -> matched {sel} ({_human_set(sel_meta['codes'], sel_meta['no_gm'])})"

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
        _p(f"\n  claim filter  = {len(wanted)} id(s); {len(latest)} matched, "
           f"{len(missing)} not found")

    claim_ids = sorted(latest)
    if opts.limit:
        claim_ids = claim_ids[: opts.limit]
    total = len(claim_ids)
    _p(f"\n══ Phase B: backfill {total} run(s) ══")

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
            _p(f"PROGRESS {i}/{total} ({int(i / total * 100) if total else 100}%)  "
               f"changed={changed} skipped={skipped} failed={failed}")

    _p("────────────────────────────────────────────────────────────")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  scanned  = {total}")
    _p(f"  changed  = {changed}")
    _p(f"  skipped  = {skipped}  (unknown gm / already correct)")
    _p(f"  failed   = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
