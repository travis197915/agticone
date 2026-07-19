#!/usr/bin/env python3
r"""Update the Timely Filing SOP scope, THEN re-run ONLY that SOP for each claim
and MERGE the result into each claim's existing run — without deleting the run
or touching any other SOP.

Two phases in ONE script (in order):
  1. WORKFLOW UPDATE (scope fix): bring Timely Filing Step 1 into scope. The
     Step 1 "No" branch carries ``is_out_of_scope=True`` + ``is_final=True`` in
     the DB, which clean-stops the whole SOP on a clean claim (no TF0/TF1
     denial). ``_fix_step1_scope`` clears both flags and rewrites that branch to
     "proceed", on the AuditDecision + the workflow's NodeRuleBinding. This runs
     BEFORE any claim eval so the engine reads the corrected scope. Idempotent;
     skip with ``--skip-scope-fix``.
  2. CLAIM RE-RUN + MERGE: re-evaluate the Timely Filing SOP against claims that
     were already audited and fold the fresh result back into their HITL runs so
     the dashboard reflects the fix.

After phase 1 the SOP proceeds into the real determination path (state/date/
county eligibility, group deadlines, history + calculator, bulletin LOB) instead
of bailing at Step 1.

It is deliberately SURGICAL (identical merge strategy to
``rerun_duplicate_verification_prod.py``):
  * The claim's ``RuleExecutionRun`` row is UPDATED in place (never deleted).
  * Every OTHER SOP's evaluation rows + trace steps are left byte-for-byte.
  * Only the Timely Filing portion is replaced:
        - its ``RuleEvaluation`` rows on the run are rewritten,
        - its EVALUATE ``ToolInvocationRecord`` rows are rewritten,
        - its steps in ``ClaimTrace.trace_json`` are spliced in,
        - ``explainability_json`` is rebuilt from the merged trace,
        - the claim's overall verdict (``final_decision_type`` / ``applied_codes``)
          and ``ClaimTrace.final_status`` are re-aggregated from the OTHER SOPs'
          persisted rows + the NEW Timely Filing rows, using the same
          deterministic precedence as the engine's aggregator (``n06_aggregate``).

Soundness note
--------------
This merge is only correct because the target workflow runs in
``execution_mode = parallel`` — every SOP is evaluated on every claim, so each
run already carries all SOPs' evaluation rows and re-aggregation from persisted
rows is faithful. The script asserts this before running.

The executive summary is regenerated per run when ``--refresh-exec-summary`` is
passed (default off) so the "Overall Claim Process Summarization" tab reflects
the merged verdict; otherwise run ``seed_executive_summaries_prod.py`` after.

Environment
-----------
PROD Postgres + LLM gateway (registry/OAuth) are baked in via ``setdefault`` so a
bare run on the prod box works; ANY real env var overrides them. To run LOCALLY
against the prod-replica DB with the DIRECT Anthropic backend:

    APP_ENV=local LLM_BACKEND=api_key \
    PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres PG_PASSWORD=postgres \
    PG_DATABASE=uhc_backend \
    python scripts/rerun_timely_filing_prod.py --claim 25XK20940100 --apply \
        --refresh-exec-summary

Usage
-----
    # preview only (no writes) — latest run per claim, all claims for the workflow
    python scripts/rerun_timely_filing_prod.py --dry-run

    # commit the merge
    python scripts/rerun_timely_filing_prod.py --apply

    # scope to one claim / a cap while testing
    python scripts/rerun_timely_filing_prod.py --apply --claim 25XK20940100
    python scripts/rerun_timely_filing_prod.py --dry-run --limit 5
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

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

# ── Placeholder: hardcode claim ids here to re-run ONLY those claims ──────────
# Leave EMPTY to process every claim for the workflow (original behaviour).
# When populated, these are used as if passed via --claim (and any --claim args
# on the command line are added on top). Example:
#     CLAIM_IDS = [
#         "25XK20940100",
#         "25XJ46879400",
#     ]
CLAIM_IDS: list[str] = [
    # "25XK20940100",
]

# ── Hardcoded PROD defaults (any real env var wins via setdefault) ────────────
_PROD_ENV = {
    "APP_ENV": "prod",
    "DJANGO_SETTINGS_MODULE": "sop_backend.settings",
    # LLM gateway / registry. NOTE: LLM_MODEL is deliberately omitted so rule
    # evaluation resolves the same model as the running prod backend.
    "LLM_BACKEND": "registry",
    "LLM_GATEWAY_TIMEOUT": "180",
    "AUTH_URL": "https://api.uhg.com/oauth2/token",
    "SCOPE": "https://api.uhg.com/.default",
    "PROJECT_ID": "a3eae3d6-90ab-4c5c-8984-f88e1bc5205d",
    "CLIENT_ID": "aa1215f1-e5dd-499a-88c9-f651567e1f22",
    "CLIENT_SECRET": "Y6VMM7xnjOHRN3xqsvJ0eFtgptUMF2sC8Bp",
    "PG_HOST": "azure-pgsql-flexibleserver-np-390744103630-dev.privatelink.postgres.database.azure.com",
    "PG_PORT": "5432",
    "PG_USER": "pgazdev",
    "PG_PASSWORD": "Xudzab-doxsoz-1vudra",
    "PG_DATABASE": "uhc_backend",
    "MONGO_URI": (
        "mongodb+srv://admin:s1Hd6lsd34jdkkljsdnflssd99ef8dfsdfc2Gl1Uu"
        "@mongoatlasagentic-pl-1.1r9aua.mongodb.net/"
        "?retryWrites=true&w=majority&serverSelectionTimeoutMS=30000"
        "&connectTimeoutMS=30000"
    ),
    "MONGO_DATABASE": "sop_ingestion_v2",
}

# Engine aggregator precedence + adverse set (mirrors n06_aggregate).
_PRECEDENCE = ["DENY", "STOP", "PEND", "REFER", "BYPASS", "WAIVE",
               "CONDITIONAL", "SYSTEM", "ALLOW"]
_ADVERSE = {"DENY", "STOP", "PEND", "REFER", "REFERRAL", "PENDED"}

# Step 1 "does not apply" branch rewrite (baked-in scope fix, see below).
_STEP1_NEW_ACTION = "Proceed to the next step to verify timely filing."


def _apply_prod_env() -> None:
    for key, val in _PROD_ENV.items():
        os.environ.setdefault(key, val)


def _load_dotenv_var(key: str) -> str:
    """Read a single var from the repo's .env WITHOUT mutating os.environ.

    Mirrors how the backend resolves the registry (settings.py loads
    ``<repo>/.env`` via python-dotenv), so a bare prod run picks up the same
    ``MODEL_REGISTRY_JSON`` the running server uses. Returns "" if not found.
    """
    for env_path in (
        os.path.join(REPO_ROOT, ".env"),
        os.path.join(os.path.dirname(REPO_ROOT), "uhc-backend", ".env"),
    ):
        if not os.path.isfile(env_path):
            continue
        try:
            from dotenv import dotenv_values
            val = dotenv_values(env_path).get(key)
        except Exception:
            val = None
        if val:
            return val.strip()
    return ""


def _resolve_model_registry() -> None:
    if os.environ.get("MODEL_REGISTRY_JSON"):
        return

    file_env = os.environ.get("MODEL_REGISTRY_FILE")
    candidates = []
    if file_env:
        candidates.append(file_env)
    candidates += [
        os.path.join(_HERE, "model_registry.json"),
        os.path.join(REPO_ROOT, "model_registry.json"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                os.environ["MODEL_REGISTRY_JSON"] = fh.read().strip()
            print(f"  MODEL_REGISTRY_JSON loaded from {path}")
            return

    from_env_file = _load_dotenv_var("MODEL_REGISTRY_JSON")
    if from_env_file:
        os.environ["MODEL_REGISTRY_JSON"] = from_env_file
        print("  MODEL_REGISTRY_JSON loaded from .env")
        return
    file_from_env_file = _load_dotenv_var("MODEL_REGISTRY_FILE")
    if file_from_env_file and os.path.isfile(file_from_env_file):
        with open(file_from_env_file, "r", encoding="utf-8") as fh:
            os.environ["MODEL_REGISTRY_JSON"] = fh.read().strip()
        print(f"  MODEL_REGISTRY_JSON loaded from {file_from_env_file} (.env)")
        return

    sys.exit(
        "ERROR: MODEL_REGISTRY_JSON is not set and no model_registry.json / .env "
        "entry was found.\n"
        "Do ONE of the following, then re-run:\n"
        "  * export MODEL_REGISTRY_JSON='{...full json...}'\n"
        "  * add MODEL_REGISTRY_JSON=... to the repo .env (same as the backend)\n"
        "  * save the full registry JSON as 'model_registry.json' next to this "
        "script\n"
        "  * run with LLM_BACKEND=api_key + ANTHROPIC_API_KEY for the direct backend"
    )


def _mask(val: str, keep: int = 6) -> str:
    if not val:
        return "(unset)"
    return val[:keep] + "…" if len(val) > keep else "…"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _emit_progress(done: int, total: int, stats: dict, t_start: float) -> None:
    elapsed = time.time() - t_start
    rate = elapsed / done if done else 0.0
    eta = rate * max(0, total - done)
    pct = (100.0 * done / total) if total else 100.0
    _p(f"PROGRESS {done}/{total} ({pct:.0f}%)  "
       f"clean={stats['clean']} defect={stats['defect']} "
       f"other={stats['other']}  updated={stats['updated']} "
       f"changed={stats['verdict_changed']} skipped={stats['skipped']} "
       f"failed={stats['failed']}  elapsed={elapsed:.0f}s eta={eta:.0f}s")


# ── Binding scoping ───────────────────────────────────────────────────────────
def _scope_loaded(loaded: dict, sop_ids: set[int]) -> dict:
    """Return a copy of ``load_workflow_bindings`` output filtered to ``sop_ids``."""
    def keep(rule: dict) -> bool:
        return rule.get("sop_id") in sop_ids

    pre = [r for r in loaded["preconditions"] if keep(r)]
    dec = [r for r in loaded["decisions"] if keep(r)]
    shapes = []
    for s in loaded["shapes"]:
        rules = [r for r in s["rules"] if keep(r)]
        if rules:
            s2 = dict(s)
            s2["rules"] = rules
            shapes.append(s2)
    keep_shapes = {s["shape_id"] for s in shapes}
    keep_keys = {r["key"] for r in (pre + dec)}
    tools_by_rule_key = {
        k: v for k, v in loaded["tools_by_rule_key"].items() if k in keep_keys
    }
    tools_by_shape = {
        sid: v for sid, v in loaded["tools_by_shape"].items() if sid in keep_shapes
    }
    all_tools = [t for t in loaded["all_tool_bindings"] if t["shape_id"] in keep_shapes]
    out = dict(loaded)
    out.update(
        preconditions=pre,
        decisions=dec,
        shapes=shapes,
        tools_by_rule_key=tools_by_rule_key,
        tools_by_shape=tools_by_shape,
        all_tool_bindings=all_tools,
    )
    return out


def _sop_rule_key(rule_key: str, sop_ids: set[int]) -> bool:
    """True when ``rule_key`` (``step:<sop>:…`` / ``pre:<sop>:…``) is a target SOP."""
    parts = (rule_key or "").split(":")
    if len(parts) < 2 or parts[0] not in ("step", "pre"):
        return False
    try:
        return int(parts[1]) in sop_ids
    except ValueError:
        return False


def _reaggregate(nontf_rows: list[dict], tf_results: list[dict]) -> tuple[str, list[str], str]:
    """Deterministic claim verdict from the merged matched set. Mirrors
    ``n06_aggregate``. Returns ``(final_decision_type, applied_codes, narrative)``."""
    merged = list(nontf_rows) + list(tf_results)
    matched = [
        r for r in merged
        if r.get("matched") and not r.get("skipped")
        and (not r.get("is_out_of_scope") or r.get("codes"))
    ]
    if not matched:
        return "ALLOW", [], "No decision rules matched; defaulting to ALLOW."

    def _is_adverse(r: dict) -> bool:
        return ((r.get("decision_type") or "").upper() in _ADVERSE
                or bool(r.get("eob_codes")))

    adverse = [r for r in matched if _is_adverse(r)]
    if not adverse:
        return (
            "ALLOW", [],
            f"No adverse disposition applied: {len(matched)} rule(s) matched, "
            f"all routing/clean. Verdict ALLOW.",
        )

    def _verdict_type(r: dict) -> str:
        dt = (r.get("decision_type") or "").upper()
        if dt in _PRECEDENCE and dt in _ADVERSE:
            return dt
        return "DENY"

    ranked = sorted(adverse, key=lambda r: _PRECEDENCE.index(_verdict_type(r)))
    winner = ranked[0]
    final_type = _verdict_type(winner)
    codes: list[str] = []
    for r in adverse:
        for c in (r.get("codes") or []):
            if c not in codes:
                codes.append(c)
    narrative = (
        f"DEFECT: {len(adverse)} matched rule(s) applied an adverse disposition "
        f"or referenced an EOB code. Highest-precedence disposition is "
        f"{final_type}."
    )
    return final_type, codes, narrative


def _persisted_eval_rows(run, tf_sop_ids: set[int]):
    """(nontf_rows_for_aggregation, tf_row_pks, tf_order_min) from the run's rows."""
    from execution_app.models import RuleEvaluation

    nontf: list[dict] = []
    tf_pks: list[int] = []
    tf_order_min = None
    for ev in RuleEvaluation.objects.filter(run=run).only(
        "id", "rule_key", "matched", "skipped", "decision_type", "codes",
        "order_index",
    ):
        if _sop_rule_key(ev.rule_key, tf_sop_ids):
            tf_pks.append(ev.id)
            if tf_order_min is None or ev.order_index < tf_order_min:
                tf_order_min = ev.order_index
            continue
        nontf.append({
            "matched": ev.matched,
            "skipped": ev.skipped,
            "decision_type": ev.decision_type or "",
            "codes": list(ev.codes or []),
            "eob_codes": [],
            "is_out_of_scope": False,
        })
    return nontf, tf_pks, (tf_order_min or 0)


# ── Scoped re-evaluation (no persistence side effects) ───────────────────────
def _evaluate_sop_only(run, workflow_id: str, tf_sop_ids: set[int]) -> dict | None:
    """Run load_bindings(scoped) -> run_tools -> execute_shapes for the Timely
    Filing SOP against the claim's stored payload. Returns the engine state
    (rule_results + tool_invocations) WITHOUT persisting anything. Memory reuse
    is disabled so the evaluation is cold."""
    import uhc_execution_engine.agents.n02_load_bindings as n02mod
    from uhc_execution_engine.agents.n03_run_tools import run_tools
    from uhc_execution_engine.agents.n_execute_shapes import execute_shapes
    from uhc_execution_engine.llm import execution_run_context
    from uhc_execution_engine.rule_loader import load_workflow_bindings as _real_load

    claim = dict(run.claim_payload or {})
    if not claim:
        return None
    claim.setdefault("claim_id", run.claim_id)
    claim.setdefault("subscriber_id", run.claim_id)

    def _scoped_loader(wf_id: str) -> dict:
        return _scope_loaded(_real_load(wf_id), tf_sop_ids)

    state: dict = {
        "workflow_id": str(workflow_id),
        "claim": claim,
        "raw_fetch": dict(run.raw_fetch or {}),
        "claim_id": run.claim_id,
        "batch_id": None,
        "stages": [],
        "tool_invocations": [],
        "tool_results": {},
        "prior_context": {},  # cold: no memory pinning / tool reuse
        "drift_entries": [],
        "status": "RUNNING",
        "run_id": str(run.id),
    }

    original = n02mod.load_workflow_bindings
    n02mod.load_workflow_bindings = _scoped_loader
    try:
        with execution_run_context(str(run.id)):
            state.update(n02mod.load_bindings(state))
            if state.get("status") == "FAILED":
                return state
            state.update(run_tools(state))
            state.update(execute_shapes(state))
    finally:
        n02mod.load_workflow_bindings = original
    return state


def _splice_trace(existing_trace: list[dict], new_tf_trace: list[dict],
                  tf_shape_ids: set[str]) -> list[dict]:
    """Replace the TF-shape entries in ``existing_trace`` with ``new_tf_trace``,
    preserving position and every other SOP's entries."""
    merged: list[dict] = []
    placed = False
    for entry in existing_trace:
        if str(entry.get("shape_id")) in tf_shape_ids:
            if not placed:
                merged.extend(new_tf_trace)
                placed = True
            continue
        merged.append(entry)
    if not placed:
        merged.extend(new_tf_trace)
    return merged


def _eval_row_kwargs(ev: dict, run, order_base: int = 0) -> dict:
    """Map an in-memory rule_results entry to RuleEvaluation constructor kwargs
    (identical to n07_persist_respond._persist)."""
    return dict(
        run=run,
        order_index=order_base + ev["order_index"],
        rule_binding_id=ev.get("binding_id") or None,
        rule_key=ev["rule_key"],
        rule_source=ev["source"].upper(),
        condition=ev["condition"],
        action=ev["action"],
        matched=ev["matched"],
        skipped=bool(ev.get("skipped")),
        skip_reason=str(ev.get("skip_reason") or "")[:255],
        confidence=ev["confidence"],
        reasoning=ev["reasoning"],
        decision_type=ev["decision_type"],
        verdict=ev["decision_type"] if ev["matched"] else "",
        codes=ev["codes"],
        tool_results_used=ev["tool_results_used"],
        live_result=ev.get("live_result"),
        overridden=bool(ev.get("overridden", False)),
        injected_context=ev.get("injected_context"),
        llm_provider=ev["llm_provider"],
        llm_ms=ev["llm_ms"],
    )


# ── Baked-in scope fix (runs BEFORE the claims) ──────────────────────────────
def _fix_step1_scope(tf_sop_ids: set[int], workflow_id: str, apply: bool) -> None:
    """Un-stick Step 1 of the Timely Filing SOP so it is IN SCOPE and proceeds.

    The Step 1 "No" branch ("This P&P does not apply.") carries BOTH
    ``is_out_of_scope=True`` and ``is_final=True`` in the DB. Either flag makes
    the execution engine clean-stop the whole SOP on a clean claim, so timely
    filing is never verified. This clears both flags on every Step 1 decision
    row that has them (the "does not apply" branch) and rewrites that branch's
    action to "proceed", on the AuditDecision AND the matching NodeRuleBinding
    (scoped to ``workflow_id``). Idempotent. This MUST run before the claim
    re-evaluation so the engine reads the corrected scope from the DB.
    """
    from django.db import transaction
    from django.db.models import Q

    from agent_tools.models import NodeRuleBinding
    from sop_ingestion.models import AuditDecision, AuditStep

    print("── Step 1 out-of-scope fix (workflow update) ────────────────")
    n_dec = 0
    n_bind = 0

    def _do() -> None:
        nonlocal n_dec, n_bind
        for sop_id in sorted(tf_sop_ids):
            step = (AuditStep.objects
                    .filter(sop_id=sop_id, step_number=1).first())
            if step is None:
                print(f"  [SKIP] sop_id={sop_id}: no Step 1.")
                continue
            rows = list(
                AuditDecision.objects.filter(step=step)
                .filter(Q(is_out_of_scope=True) | Q(is_final=True))
                .order_by("row_index")
            )
            if not rows:
                print(f"  SOP {sop_id}: Step 1 already in scope — nothing to fix.")
                continue
            print(f"  SOP {sop_id}  step_id={step.id}  Q={step.question[:48]!r}")
            for d in rows:
                print(f"      row{d.row_index} [{d.subrule_id}] "
                      f"oos {d.is_out_of_scope}->False  final {d.is_final}->False"
                      f"  IF={d.condition_if!r}")
                d.is_out_of_scope = False
                d.is_final = False
                if "does not apply" in (d.action_text or "").lower():
                    d.action_text = _STEP1_NEW_ACTION
                    d.action_summary = _STEP1_NEW_ACTION
                if apply:
                    d.save(update_fields=[
                        "is_out_of_scope", "is_final",
                        "action_text", "action_summary"])
                n_dec += 1

                rule_key = f"step:{sop_id}:1:{d.row_index}"
                bq = NodeRuleBinding.objects.filter(
                    sop_id=sop_id, rule_key=rule_key,
                    shape__workbench__work_area__workflow_id=workflow_id)
                for b in bq:
                    if "does not apply" not in (b.action or "").lower():
                        continue
                    print(f"        binding {str(b.shape_id)[:8]} "
                          f"action -> {_STEP1_NEW_ACTION!r}")
                    b.action = _STEP1_NEW_ACTION
                    if apply:
                        b.save(update_fields=["action"])
                    n_bind += 1

    if apply:
        with transaction.atomic():
            _do()
        print(f"  APPLIED scope fix: {n_dec} decision row(s), "
              f"{n_bind} binding(s).")
    else:
        _do()
        print(f"  DRY-RUN scope fix: would clear {n_dec} decision row(s) and "
              f"rewrite {n_bind} binding(s). (Claim previews below still reflect "
              f"CURRENT DB scope until --apply commits this.)")
    print("────────────────────────────────────────────────────────────")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Update the Timely Filing SOP scope (Step 1) THEN re-run "
                    "only that SOP and merge into each claim's existing run "
                    "(in place, non-destructive)."
    )
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID,
                    help=f"Workflow id (default {DEFAULT_WORKFLOW_ID}).")
    ap.add_argument("--sop-id", type=int, action="append", default=[],
                    help="Force a Timely Filing SOP id (repeatable). Default: "
                         "auto-detect bound SOPs whose title contains 'timely'.")
    ap.add_argument("--batch", default="",
                    help="Only consider runs from this batch id.")
    ap.add_argument("--claim", action="append", default=[],
                    help="Only these claim id(s) (repeatable).")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap number of claims processed (0 = all).")
    ap.add_argument("--apply", action="store_true",
                    help="Commit the merge. Without it the script is a dry-run.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Explicit dry-run (default when --apply is absent).")
    ap.add_argument("--refresh-exec-summary", action="store_true",
                    help="After applying, regenerate the ClaimExecutiveSummary "
                         "for each updated run so the 'Overall Claim Process "
                         "Summarization' tab reflects the merged verdict.")
    ap.add_argument("--skip-scope-fix", action="store_true",
                    help="Do NOT apply the Step 1 out-of-scope fix first "
                         "(default: apply it before running the claims).")
    opts = ap.parse_args()
    apply = opts.apply and not opts.dry_run

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    _apply_prod_env()
    # Registry JSON is only needed for the gateway backend. With the direct
    # Anthropic/OpenAI backend (LLM_BACKEND=api_key) a local run just needs
    # ANTHROPIC_API_KEY in the environment.
    if os.environ.get("LLM_BACKEND") == "registry":
        _resolve_model_registry()

    print("── Timely-Filing re-run + merge ─────────────────────────────")
    print(f"  mode        = {'APPLY (writes)' if apply else 'DRY-RUN (no writes)'}")
    print(f"  APP_ENV     = {os.environ.get('APP_ENV')}")
    print(f"  LLM_BACKEND = {os.environ.get('LLM_BACKEND')}")
    print(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    print(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    print(f"  CLIENT_ID   = {_mask(os.environ.get('CLIENT_ID', ''), 8)}")
    print(f"  registry    = {len(os.environ.get('MODEL_REGISTRY_JSON', ''))} chars")
    print(f"  workflow    = {opts.workflow}")
    print("────────────────────────────────────────────────────────────")

    import django

    django.setup()

    from django.db import transaction
    from django.utils import timezone

    from builder.models import Workflow
    from execution_app import trace_builder
    from execution_app.models import (ClaimTrace, RuleEvaluation,
                                       RuleExecutionRun, ToolInvocationRecord)
    from execution_app.trace_builder import _iso, build_trace  # type: ignore
    from execution_app.trace_builder import _build_explainability  # type: ignore
    from uhc_execution_engine.rule_loader import load_workflow_bindings

    wf = Workflow.objects.filter(id=opts.workflow).first()
    if wf is None:
        sys.exit(f"ERROR: workflow {opts.workflow} not found in this DB.")
    mode = str((wf.metadata or {}).get("execution_mode") or "linear").lower()
    print(f"execution_mode = {mode}")
    if mode != "parallel":
        sys.exit(
            "ERROR: this merge is only sound for parallel workflows (every SOP "
            "runs on every claim). This workflow is "
            f"'{mode}'. A linear workflow short-circuits on the first defect, so "
            "the other SOPs may be missing from denied claims — use a FULL "
            "re-run instead. Aborting."
        )

    # Resolve the Timely Filing SOP id(s) bound to this workflow + their shapes.
    loaded = load_workflow_bindings(opts.workflow)
    all_rules = loaded["decisions"] + loaded["preconditions"]
    if opts.sop_id:
        tf_sop_ids = set(opts.sop_id)
    else:
        tf_sop_ids = {
            r["sop_id"] for r in all_rules
            if "timely" in (r.get("sop_title") or "").lower()
        }
    if not tf_sop_ids:
        sys.exit("ERROR: could not identify a Timely Filing SOP bound to this "
                 "workflow. Pass --sop-id explicitly.")
    tf_shape_ids = {
        str(r["shape_id"]) for r in all_rules if r["sop_id"] in tf_sop_ids
    }
    tf_tool_binding_ids = {
        t["binding_id"] for t in loaded["all_tool_bindings"]
        if str(t["shape_id"]) in tf_shape_ids
    }
    titles = sorted({
        r.get("sop_title") or "" for r in all_rules if r["sop_id"] in tf_sop_ids
    })
    print(f"timely-filing SOP id(s) = {sorted(tf_sop_ids)}")
    for t in titles:
        print(f"  title: {t}")
    print(f"timely-filing shape(s)  = {len(tf_shape_ids)}")
    print("────────────────────────────────────────────────────────────")

    # STEP 1 of the workflow update: bring Timely Filing Step 1 into scope so
    # the SOP actually runs. Done BEFORE any claim eval so the engine reads the
    # corrected scope. Skippable / idempotent.
    if opts.skip_scope_fix:
        print("── Step 1 out-of-scope fix SKIPPED (--skip-scope-fix) ───────")
    else:
        _fix_step1_scope(tf_sop_ids, opts.workflow, apply)

    # Candidate runs: latest terminal run per claim for this workflow.
    # Claim scope = hardcoded CLAIM_IDS placeholder + any --claim args.
    claim_filter = list(dict.fromkeys([*CLAIM_IDS, *opts.claim]))
    if claim_filter:
        print(f"claim scope       = {len(claim_filter)} hardcoded/CLI id(s)")
    runs_qs = RuleExecutionRun.objects.filter(workflow_id=opts.workflow)
    if opts.batch:
        runs_qs = runs_qs.filter(batch_id=opts.batch)
    if claim_filter:
        runs_qs = runs_qs.filter(claim_id__in=claim_filter)

    latest_by_claim: dict[str, RuleExecutionRun] = {}
    for run in runs_qs.order_by("claim_id", "-started_at"):
        if run.claim_id and run.claim_id not in latest_by_claim:
            latest_by_claim[run.claim_id] = run
    claims = sorted(latest_by_claim)
    if opts.limit:
        claims = claims[: opts.limit]
    print(f"claims to process = {len(claims)}")
    print("────────────────────────────────────────────────────────────")

    stats = {"processed": 0, "updated": 0, "skipped": 0, "failed": 0,
             "verdict_changed": 0, "clean": 0, "defect": 0, "other": 0}
    t_start = time.time()
    total = len(claims)

    for i, cid in enumerate(claims, start=1):
        run = latest_by_claim[cid]
        prefix = f"[{i}/{total}] claim={cid} run={run.id}"
        try:
            if run.status not in ("COMPLETED", "TERMINATED_EARLY"):
                _p(f"{prefix} SKIP (status={run.status}; no full audit)")
                stats["skipped"] += 1
                continue
            trace_row = ClaimTrace.objects.filter(run=run).first()
            if trace_row is None or not trace_row.trace_json:
                _p(f"{prefix} SKIP (no ClaimTrace to merge into)")
                stats["skipped"] += 1
                continue
            if not (run.claim_payload or {}):
                _p(f"{prefix} SKIP (no stored claim_payload to re-run offline)")
                stats["skipped"] += 1
                continue

            state = _evaluate_sop_only(run, opts.workflow, tf_sop_ids)
            if state is None or state.get("status") == "FAILED":
                msg = (state or {}).get("error_message", "no state")
                _p(f"{prefix} FAILED tf re-eval: {msg}")
                stats["failed"] += 1
                continue

            new_results = list(state.get("rule_results") or [])
            new_tools = list(state.get("tool_invocations") or [])
            if not new_results:
                _p(f"{prefix} SKIP (tf SOP produced no evaluations)")
                stats["skipped"] += 1
                continue

            new_tf_trace, _ = build_trace(run, new_results, new_tools)
            live_tf_shapes = {str(e.get("shape_id")) for e in new_tf_trace} \
                or tf_shape_ids

            merged_trace = _splice_trace(
                list(trace_row.trace_json or []), new_tf_trace, live_tf_shapes,
            )
            explainability = _build_explainability(
                merged_trace, str(run.id), run.claim_id or "",
                _iso(run.started_at), _iso(run.finished_at), run,
            )

            nontf_rows, tf_pks, tf_order_base = _persisted_eval_rows(run, tf_sop_ids)
            new_final, new_codes, new_narr = _reaggregate(nontf_rows, new_results)
            new_status = (trace_builder.normalize_decision(new_final)
                          or trace_builder.claim_status(merged_trace))

            old_final = run.final_decision_type or ""
            old_status = trace_row.final_status or ""
            changed = (new_final != old_final) or (new_status != old_status)
            if changed:
                stats["verdict_changed"] += 1
            if new_status == trace_builder.CLEAN:
                stats["clean"] += 1
            elif new_status == trace_builder.DEFECT:
                stats["defect"] += 1
            else:
                stats["other"] += 1

            tf_new = trace_builder.node_audit_status([e for e in new_results])
            _p(f"{prefix} tf={tf_new} verdict {old_final or '-'}->{new_final} "
               f"status {old_status or '-'}->{new_status} "
               f"{'[CHANGED]' if changed else ''}")

            if not apply:
                stats["processed"] += 1
                continue

            with transaction.atomic():
                if tf_pks:
                    RuleEvaluation.objects.filter(id__in=tf_pks).delete()
                RuleEvaluation.objects.bulk_create([
                    RuleEvaluation(**_eval_row_kwargs(ev, run, tf_order_base))
                    for ev in new_results
                ])

                if tf_tool_binding_ids:
                    ToolInvocationRecord.objects.filter(
                        run=run, phase="EVALUATE",
                        tool_binding_id__in=list(tf_tool_binding_ids),
                    ).delete()
                ToolInvocationRecord.objects.bulk_create([
                    ToolInvocationRecord(
                        run=run,
                        tool_binding_id=inv.get("binding_id") or None,
                        tool_name=inv["tool_name"],
                        phase=inv.get("phase", "EVALUATE"),
                        args=inv.get("args") or {},
                        ok=inv["ok"],
                        result=inv.get("result") if isinstance(inv.get("result"), (dict, list))
                               else {"value": inv.get("result")},
                        error=inv.get("error", ""),
                        duration_ms=inv.get("duration_ms", 0),
                        reused_from_run=inv.get("reused_from_run") or None,
                    )
                    for inv in new_tools
                ])

                RuleExecutionRun.objects.filter(id=run.id).update(
                    final_decision_type=new_final,
                    applied_codes=new_codes,
                    narrative=new_narr,
                )
                ClaimTrace.objects.filter(run=run).update(
                    final_status=new_status,
                    trace_json=merged_trace,
                    explainability_json=explainability,
                    updated_at=timezone.now(),
                )
            stats["processed"] += 1
            stats["updated"] += 1

            if opts.refresh_exec_summary:
                try:
                    from execution_app.executive_summary import generate_for_run
                    run.refresh_from_db()
                    generate_for_run(run, source="backfill", force=True)
                    _p(f"{prefix} exec-summary refreshed")
                except Exception as exc:
                    _p(f"{prefix} exec-summary refresh FAILED: {exc}")
        except Exception as exc:
            _p(f"{prefix} ERROR: {exc}")
            traceback.print_exc()
            stats["failed"] += 1
        finally:
            _emit_progress(i, total, stats, t_start)

    dur = time.time() - t_start
    _p("────────────────────────────────────────────────────────────")
    _p(f"Done in {dur:.1f}s  ({'APPLIED' if apply else 'DRY-RUN'}).")
    _p(f"  processed        = {stats['processed']}")
    _p(f"  updated          = {stats['updated']}")
    _p(f"  verdict changed  = {stats['verdict_changed']}")
    _p(f"  clean            = {stats['clean']}")
    _p(f"  defect           = {stats['defect']}")
    _p(f"  other/inconcl.   = {stats['other']}")
    _p(f"  skipped          = {stats['skipped']}")
    _p(f"  failed           = {stats['failed']}")


if __name__ == "__main__":
    main()
