#!/usr/bin/env python3
r"""Re-run ONLY the Duplicate Verification SOP for every claim and MERGE the
result into each claim's existing run — without deleting the run or touching
any other SOP.

Why this exists
---------------
``fix_duplicate_step2_rules.py`` repaired Step 2 of the Duplicate Verification
SOP so its criteria (Provider NPI/TIN, Member, DOS, Procedure) are actually
evaluated and routed. This script re-evaluates that SOP against the claims that
were already audited and folds the fresh Duplicate-Verification result back into
their HITL runs so the dashboard reflects the fix.

It is deliberately SURGICAL:
  * The claim's ``RuleExecutionRun`` row is UPDATED in place (never deleted).
  * Every OTHER SOP's evaluation rows + trace steps are left byte-for-byte.
  * Only the Duplicate-Verification portion is replaced:
        - its ``RuleEvaluation`` rows on the run are rewritten,
        - its EVALUATE ``ToolInvocationRecord`` rows are rewritten,
        - its steps in ``ClaimTrace.trace_json`` are spliced in,
        - ``explainability_json`` is rebuilt from the merged trace,
        - the claim's overall verdict (``final_decision_type`` / ``applied_codes``)
          and ``ClaimTrace.final_status`` are re-aggregated from the OTHER SOPs'
          persisted rows + the NEW Duplicate rows, using the same deterministic
          precedence as the engine's aggregator (``n06_aggregate``).

Soundness note
--------------
This merge is only correct because the target workflow runs in
``execution_mode = parallel`` — every SOP is evaluated on every claim, so each
run already carries all SOPs' evaluation rows and re-aggregation from persisted
rows is faithful. The script asserts this before running. The one intentional
approximation vs. a full re-run: the aggregator's *tool-failure* nuance
(a failed coverage tool → REFER, other failed tool → INCONCLUSIVE) is not
reconstructed for the OTHER SOPs from persisted rows; a claim whose ONLY finding
was such a tool-failure verdict is re-aggregated as ALLOW. The Duplicate SOP
itself is fully re-evaluated live, so its tool failures ARE honoured.

The executive summary is NOT regenerated here — run
``seed_executive_summaries_prod.py`` afterwards if you want the summaries to
reflect the merged verdicts.

Environment
-----------
PROD Postgres + LLM gateway (registry/OAuth) are baked in via ``setdefault`` so a
bare run on the Windows box works; ANY real env var overrides them. ``LLM_MODEL``
is intentionally NOT forced, so rule evaluation uses the same model resolution
as the running prod backend. ``MODEL_REGISTRY_JSON`` is resolved (in order) from
the env var, a ``model_registry.json`` sidecar file, or the repo ``.env`` — the
SAME file the backend loads — so a bare prod run needs no manual export.

Usage
-----
    # preview only (no writes) — latest run per claim, all claims for the workflow
    python .\scripts\rerun_duplicate_verification_prod.py --dry-run

    # commit the merge
    python .\scripts\rerun_duplicate_verification_prod.py --apply

    # scope to one batch / a few claims / a cap while testing
    python .\scripts\rerun_duplicate_verification_prod.py --apply --batch <BATCH_UUID>
    python .\scripts\rerun_duplicate_verification_prod.py --apply --claim 25XJ46879400
    python .\scripts\rerun_duplicate_verification_prod.py --dry-run --limit 5

    # local run against the prod-replica DB
    $env:PG_HOST="127.0.0.1"; $env:PG_PORT="5433"; $env:PG_USER="postgres"
    $env:PG_PASSWORD="postgres"; $env:PG_DATABASE="uhc_backend"
    python .\scripts\rerun_duplicate_verification_prod.py --dry-run
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
#         "25XJ46879400",
#         "25XK20940100",
#     ]
CLAIM_IDS: list[str] = [
    # "25XJ46879400",
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


def _apply_prod_env() -> None:
    for key, val in _PROD_ENV.items():
        os.environ.setdefault(key, val)


def _load_dotenv_var(key: str) -> str:
    """Read a single var from the repo's .env WITHOUT mutating os.environ.

    This mirrors how the backend resolves the registry (settings.py loads
    ``<repo>/.env`` via python-dotenv), so a bare prod run picks up the same
    ``MODEL_REGISTRY_JSON`` the running server uses — no manual export needed.
    Returns "" if not found.
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

    # 1) explicit MODEL_REGISTRY_FILE / model_registry.json sidecar file
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

    # 2) the repo .env (same source the running prod backend uses)
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
        "  * $env:MODEL_REGISTRY_JSON = '{...full json...}'\n"
        "  * add MODEL_REGISTRY_JSON=... to the repo .env (same as the backend)\n"
        "  * save the full registry JSON as 'model_registry.json' next to this "
        "script\n"
        "  * $env:MODEL_REGISTRY_FILE = 'C:\\path\\to\\registry.json'"
    )


def _mask(val: str, keep: int = 6) -> str:
    if not val:
        return "(unset)"
    return val[:keep] + "…" if len(val) > keep else "…"


def _p(msg: str = "") -> None:
    """print() that flushes immediately so progress shows live in server logs
    even when stdout is piped/redirected."""
    print(msg, flush=True)


def _emit_progress(done: int, total: int, stats: dict, t_start: float) -> None:
    """One live heartbeat line after each claim: how many are done + running
    clean/defect tally (all claims are expected CLEAN, so a rising ``defect``
    count is the signal to look at)."""
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


def _dup_rule_key(rule_key: str, sop_ids: set[int]) -> bool:
    """True when ``rule_key`` (``step:<sop>:…`` / ``pre:<sop>:…``) is a dup SOP."""
    parts = (rule_key or "").split(":")
    if len(parts) < 2 or parts[0] not in ("step", "pre"):
        return False
    try:
        return int(parts[1]) in sop_ids
    except ValueError:
        return False


def _reaggregate(nondup_rows: list[dict], dup_results: list[dict]) -> tuple[str, list[str], str]:
    """Deterministic claim verdict from the merged matched set.

    Mirrors ``n06_aggregate`` (minus the tool-failure nuance for the OTHER SOPs,
    which cannot be reconstructed from persisted rows). Returns
    ``(final_decision_type, applied_codes, narrative)``.
    """
    merged = list(nondup_rows) + list(dup_results)
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


def _persisted_eval_rows(run, dup_sop_ids: set[int]):
    """(nondup_rows_for_aggregation, dup_row_pks) from the run's RuleEvaluation."""
    from execution_app.models import RuleEvaluation

    nondup: list[dict] = []
    dup_pks: list[int] = []
    dup_order_min = None
    for ev in RuleEvaluation.objects.filter(run=run).only(
        "id", "rule_key", "matched", "skipped", "decision_type", "codes",
        "order_index",
    ):
        if _dup_rule_key(ev.rule_key, dup_sop_ids):
            dup_pks.append(ev.id)
            if dup_order_min is None or ev.order_index < dup_order_min:
                dup_order_min = ev.order_index
            continue
        nondup.append({
            "matched": ev.matched,
            "skipped": ev.skipped,
            "decision_type": ev.decision_type or "",
            "codes": list(ev.codes or []),
            "eob_codes": [],
            "is_out_of_scope": False,
        })
    return nondup, dup_pks, (dup_order_min or 0)


# ── Scoped re-evaluation (no persistence side effects) ───────────────────────
def _evaluate_dup_only(run, workflow_id: str, dup_sop_ids: set[int]) -> dict | None:
    """Run load_bindings(scoped) -> run_tools -> execute_shapes for the dup SOP
    against the claim's stored payload. Returns the engine state (rule_results +
    tool_invocations) WITHOUT persisting anything. Memory reuse is disabled so
    the evaluation is cold (prior verdicts never pin the outcome)."""
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
        return _scope_loaded(_real_load(wf_id), dup_sop_ids)

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
        # Stamp LLMCallLog rows onto the existing run (valid FK) for cost/telemetry.
        with execution_run_context(str(run.id)):
            state.update(n02mod.load_bindings(state))
            if state.get("status") == "FAILED":
                return state
            state.update(run_tools(state))
            state.update(execute_shapes(state))
    finally:
        n02mod.load_workflow_bindings = original
    return state


def _splice_trace(existing_trace: list[dict], new_dup_trace: list[dict],
                  dup_shape_ids: set[str]) -> list[dict]:
    """Replace the dup-shape entries in ``existing_trace`` with ``new_dup_trace``,
    preserving position and every other SOP's entries."""
    merged: list[dict] = []
    placed = False
    for entry in existing_trace:
        if str(entry.get("shape_id")) in dup_shape_ids:
            if not placed:
                merged.extend(new_dup_trace)
                placed = True
            # drop stale dup entries
            continue
        merged.append(entry)
    if not placed:
        merged.extend(new_dup_trace)
    return merged


def _eval_row_kwargs(ev: dict, run, order_base: int = 0) -> dict:
    """Map an in-memory rule_results entry to RuleEvaluation constructor kwargs
    (identical to n07_persist_respond._persist). ``order_base`` shifts the fresh
    dup rows to roughly where the old dup rows sat within the run."""
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


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Re-run only the Duplicate Verification SOP and merge into "
                    "each claim's existing run (in place, non-destructive)."
    )
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID,
                    help=f"Workflow id (default {DEFAULT_WORKFLOW_ID}).")
    ap.add_argument("--sop-id", type=int, action="append", default=[],
                    help="Force a Duplicate SOP id (repeatable). Default: "
                         "auto-detect bound SOPs whose title contains 'duplicate'.")
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
    opts = ap.parse_args()
    apply = opts.apply and not opts.dry_run

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    _apply_prod_env()
    # Registry JSON is only needed for the gateway backend. When running with
    # the direct Anthropic/OpenAI backend (LLM_BACKEND=api_key), skip it so a
    # local run just needs ANTHROPIC_API_KEY in the environment.
    if os.environ.get("LLM_BACKEND") == "registry":
        _resolve_model_registry()

    print("── Duplicate-Verification re-run + merge ────────────────────")
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

    # Resolve the Duplicate SOP id(s) bound to this workflow + their shapes.
    loaded = load_workflow_bindings(opts.workflow)
    all_rules = loaded["decisions"] + loaded["preconditions"]
    if opts.sop_id:
        dup_sop_ids = set(opts.sop_id)
    else:
        dup_sop_ids = {
            r["sop_id"] for r in all_rules
            if "duplicate" in (r.get("sop_title") or "").lower()
        }
    if not dup_sop_ids:
        sys.exit("ERROR: could not identify a Duplicate Verification SOP bound to "
                 "this workflow. Pass --sop-id explicitly.")
    dup_shape_ids = {
        str(r["shape_id"]) for r in all_rules if r["sop_id"] in dup_sop_ids
    }
    dup_tool_binding_ids = {
        t["binding_id"] for t in loaded["all_tool_bindings"]
        if str(t["shape_id"]) in dup_shape_ids
    }
    titles = sorted({
        r.get("sop_title") or "" for r in all_rules if r["sop_id"] in dup_sop_ids
    })
    print(f"duplicate SOP id(s) = {sorted(dup_sop_ids)}")
    for t in titles:
        print(f"  title: {t}")
    print(f"duplicate shape(s)  = {len(dup_shape_ids)}")
    print("────────────────────────────────────────────────────────────")

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

            state = _evaluate_dup_only(run, opts.workflow, dup_sop_ids)
            if state is None or state.get("status") == "FAILED":
                msg = (state or {}).get("error_message", "no state")
                _p(f"{prefix} FAILED dup re-eval: {msg}")
                stats["failed"] += 1
                continue

            new_results = list(state.get("rule_results") or [])
            new_tools = list(state.get("tool_invocations") or [])
            if not new_results:
                _p(f"{prefix} SKIP (dup SOP produced no evaluations)")
                stats["skipped"] += 1
                continue

            # Build the fresh dup trace (using the existing run for ids/timestamps).
            new_dup_trace, _ = build_trace(run, new_results, new_tools)
            live_dup_shapes = {str(e.get("shape_id")) for e in new_dup_trace} \
                or dup_shape_ids

            merged_trace = _splice_trace(
                list(trace_row.trace_json or []), new_dup_trace, live_dup_shapes,
            )
            explainability = _build_explainability(
                merged_trace, str(run.id), run.claim_id or "",
                _iso(run.started_at), _iso(run.finished_at), run,
            )

            # Re-aggregate the claim verdict from OTHER SOPs (persisted) + NEW dup.
            nondup_rows, dup_pks, dup_order_base = _persisted_eval_rows(run, dup_sop_ids)
            new_final, new_codes, new_narr = _reaggregate(nondup_rows, new_results)
            new_status = (trace_builder.normalize_decision(new_final)
                          or trace_builder.claim_status(merged_trace))

            old_final = run.final_decision_type or ""
            old_status = trace_row.final_status or ""
            changed = (new_final != old_final) or (new_status != old_status)
            if changed:
                stats["verdict_changed"] += 1
            # Running clean/defect tally. All claims are expected CLEAN, so a
            # non-CLEAN outcome is loud (ANOMALY) for quick eyeballing.
            if new_status == trace_builder.CLEAN:
                stats["clean"] += 1
            elif new_status == trace_builder.DEFECT:
                stats["defect"] += 1
            else:
                stats["other"] += 1

            dup_new = trace_builder.node_audit_status([
                e for e in new_results
            ])
            anomaly = "  <<< ANOMALY (expected CLEAN)" \
                if new_status != trace_builder.CLEAN else ""
            _p(f"{prefix} dup={dup_new} verdict {old_final or '-'}->{new_final} "
               f"status {old_status or '-'}->{new_status} "
               f"{'[CHANGED]' if changed else ''}{anomaly}")

            if not apply:
                stats["processed"] += 1
                continue

            with transaction.atomic():
                # Replace ONLY the dup SOP's evaluation rows on this run.
                if dup_pks:
                    RuleEvaluation.objects.filter(id__in=dup_pks).delete()
                RuleEvaluation.objects.bulk_create([
                    RuleEvaluation(**_eval_row_kwargs(ev, run, dup_order_base))
                    for ev in new_results
                ])

                # Replace the dup SOP's EVALUATE tool-invocation rows.
                if dup_tool_binding_ids:
                    ToolInvocationRecord.objects.filter(
                        run=run, phase="EVALUATE",
                        tool_binding_id__in=list(dup_tool_binding_ids),
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

                # Update the run verdict + trace in place (run row is NOT deleted).
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
                except Exception as exc:  # never let the summary abort the merge
                    _p(f"{prefix} exec-summary refresh FAILED: {exc}")
        except Exception as exc:  # keep going; one bad claim must not abort the batch
            _p(f"{prefix} ERROR: {exc}")
            traceback.print_exc()
            stats["failed"] += 1
        finally:
            # Heartbeat on EVERY path (updated / skipped / failed) so the server
            # log always shows how many claims are done + the clean/defect tally.
            _emit_progress(i, total, stats, t_start)

    dur = time.time() - t_start
    _p("────────────────────────────────────────────────────────────")
    _p(f"Done in {dur:.1f}s  ({'APPLIED' if apply else 'DRY-RUN'}).")
    _p(f"  processed        = {stats['processed']}")
    _p(f"  updated          = {stats['updated']}")
    _p(f"  verdict changed  = {stats['verdict_changed']}")
    _p(f"  clean            = {stats['clean']}")
    _p(f"  defect           = {stats['defect']}  (expected 0 — all claims clean)")
    _p(f"  other/inconcl.   = {stats['other']}")
    _p(f"  skipped          = {stats['skipped']}")
    _p(f"  failed           = {stats['failed']}")


if __name__ == "__main__":
    main()
