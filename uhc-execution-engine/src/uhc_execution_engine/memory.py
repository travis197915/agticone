"""Per-claim persistent context (ClaimMemory) — load, reuse, resolve, update.

Memory is scoped per ``(claim_id, sop)``: one row per SOP the claim has been
audited against, regardless of which workflow chained that SOP. Every run:

* loads all of the claim's memory rows and reuses EVALUATE-phase tool results
  within a freshness TTL (args-hash keyed, merged across SOP rows);
* injects the prior per-rule verdict + reasoning into rule prompts as
  awareness-only context (the agent must never reference it in output);
* on structured drift (a matched/skipped flip for the same rule against
  unchanged claim data) resolves per ``CLAIM_MEMORY_CONFLICT_POLICY`` —
  ``prior_wins`` adopts the remembered verdict and stashes the live one in
  ``RuleEvaluation.live_result``;
* after a successful persist, rebuilds each touched SOP's memory row from the
  adopted results.

Everything here is fail-open: any exception is logged and the run proceeds as
if no memory existed. A memory problem must never fail a claim.

Carve-out: each row remembers the claim payload hash from the run that wrote
it. When the freshly built claim hashes differently, that SOP's tool reuse and
prior-wins pinning are suspended for the run and its row rebuilds from live
results. Rules with no SOP (``sop_id`` empty) share a single "" row.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from .config import EngineConfig

logger = logging.getLogger(__name__)

# Caps so the memory rows and the injected prompt blocks stay small.
_REASONING_CAP = 500
_NARRATIVE_CAP = 1000
_RUN_HISTORY_CAP = 20
_DRIFT_CAP = 50

_SUCCESS_STATUSES = {"COMPLETED", "TERMINATED_EARLY"}


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def payload_hash(claim: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(claim or {}).encode()).hexdigest()


def args_hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(args or {}).encode()).hexdigest()


def tool_memory_key(tool_name: str, args: dict[str, Any]) -> str:
    return f"{tool_name}:{args_hash(args)}"


def sop_key(sop_id: Any) -> str:
    """Canonical string key for an AuditSop id ('' for rules with no SOP)."""
    return "" if sop_id in (None, "") else str(sop_id)


def _now() -> datetime:
    return datetime.now(dt_timezone.utc)


# ── Load ─────────────────────────────────────────────────────────────────────

def load_prior_context(cfg: EngineConfig, *, claim_id: str,
                       claim: dict[str, Any]) -> dict[str, Any]:
    """Build the ``prior_context`` state field for one run from every memory
    row of this claim. ``{}`` when memory is disabled, absent, or unreadable.

    Shape::

        {
          "payload_changed_by_sop": {sop_key: bool},
          "rule_memory":            {sop_key: {rule_key: entry}},
          "tool_memory":            {tool_key: entry},   # merged, fresh rows only
          "tool_memory_by_sop":     {sop_key: {tool_key: entry}},
          "last_decision_type" / "last_narrative" / "last_applied_codes":
              claim-level final output from the most recently updated row,
          "payload_changed":        that row's changed flag (claim-level),
          "runs_count":             that row's runs_count,
        }
    """
    if not cfg.claim_memory_enabled or not claim_id:
        return {}
    try:
        from execution_app.models import ClaimMemory

        rows = list(ClaimMemory.objects.filter(claim_id=claim_id))
        if not rows:
            return {}
        current_hash = payload_hash(claim)

        rule_memory: dict[str, dict[str, Any]] = {}
        tool_memory_by_sop: dict[str, dict[str, Any]] = {}
        merged_tools: dict[str, Any] = {}
        changed_by_sop: dict[str, bool] = {}
        for row in rows:
            skey = sop_key(row.sop_id)
            changed = bool(row.claim_payload_hash
                           and row.claim_payload_hash != current_hash)
            changed_by_sop[skey] = changed
            rule_memory[skey] = dict(row.rule_memory or {})
            tool_memory_by_sop[skey] = dict(row.tool_memory or {})
            if not changed:
                # Tools from a stale row were computed against old claim
                # data — exclude them from the reuse lookup.
                merged_tools.update(row.tool_memory or {})
            else:
                logger.info(
                    "claim_memory claim=%s sop=%s payload changed since last "
                    "run — tool reuse + prior-wins pinning suspended for this "
                    "SOP", claim_id, skey or "-")

        # Claim-level final output comes from the most recently updated row.
        latest = max(rows, key=lambda r: r.updated_at)
        history = list(latest.run_history or [])
        last_hist = history[-1] if history and isinstance(history[-1], dict) else {}
        last_codes = list(last_hist.get("codes") or [])
        return {
            # True until the first engine run stamps this claim: the last
            # output came from a legacy seed, whose decision vocabulary is an
            # import mapping — good for prompt context, not for adoption.
            "seeded_last_output": last_hist.get("source") == "legacy_seed",
            "runs_count": latest.runs_count,
            "last_run_id": str(latest.last_run_id) if latest.last_run_id else "",
            "last_decision_type": latest.last_decision_type,
            "last_narrative": latest.last_narrative,
            "last_applied_codes": last_codes,
            "payload_changed": changed_by_sop.get(sop_key(latest.sop_id), False),
            "payload_changed_by_sop": changed_by_sop,
            "rule_memory": rule_memory,
            "tool_memory": merged_tools,
            "tool_memory_by_sop": tool_memory_by_sop,
        }
    except Exception:
        logger.exception("claim_memory: load failed for claim=%s — running cold",
                         claim_id)
        return {}


# ── Tool reuse ───────────────────────────────────────────────────────────────

def lookup_reusable_tool(cfg: EngineConfig, prior_context: dict[str, Any],
                         tool_name: str, args: dict[str, Any]
                         ) -> dict[str, Any] | None:
    """Return a remembered EVALUATE-phase tool entry usable in place of a live
    call, or ``None``. Entries from SOP rows whose claim data changed were
    already excluded at load time."""
    if not cfg.claim_memory_enabled or not prior_context:
        return None
    entry = (prior_context.get("tool_memory") or {}).get(
        tool_memory_key(tool_name, args))
    # isinstance guard: a corrupt/hand-edited row must run cold, not crash.
    if not isinstance(entry, dict) or not entry.get("ok") \
            or entry.get("phase") != "EVALUATE":
        return None
    try:
        called_at = datetime.fromisoformat(entry["called_at"])
        if called_at.tzinfo is None:
            called_at = called_at.replace(tzinfo=dt_timezone.utc)
    except Exception:
        return None
    if _now() - called_at > timedelta(hours=cfg.claim_memory_tool_ttl_hours):
        return None
    return entry


# ── Conflict resolution (rule level) ────────────────────────────────────────

def resolve_rule_conflict(cfg: EngineConfig, prior_context: dict[str, Any],
                          rule_key: str, *, rule_sop_id: Any = None,
                          live_matched: bool,
                          live_skipped: bool) -> dict[str, Any] | None:
    """Decide whether the prior verdict should replace the live one.

    Returns the prior rule_memory entry to adopt, or ``None`` to keep the live
    result. Adoption requires: memory enabled, policy=prior_wins, unchanged
    claim data for this rule's SOP, a remembered entry for this rule, and a
    structured flip (matched/skipped differ). Reasoning-text variance alone is
    never drift.
    """
    if not cfg.claim_memory_enabled or not prior_context:
        return None
    if cfg.claim_memory_conflict_policy != "prior_wins":
        return None
    skey = sop_key(rule_sop_id)
    if (prior_context.get("payload_changed_by_sop") or {}).get(skey):
        return None
    sop_rules = (prior_context.get("rule_memory") or {}).get(skey)
    prior = sop_rules.get(rule_key) if isinstance(sop_rules, dict) else None
    if not isinstance(prior, dict):
        return None
    if prior.get("seeded"):
        # Legacy-seeded entries are injection-only context: they were imported
        # at step granularity (one verdict fanned across a step's decision
        # rows), too coarse to pin per-rule. The first engine run rebuilds the
        # row at full granularity; pinning activates from the next run.
        return None
    if (bool(prior.get("matched")) == live_matched
            and bool(prior.get("skipped")) == live_skipped):
        return None
    return prior


def prior_rule_entry(prior_context: dict[str, Any], rule_key: str,
                     rule_sop_id: Any = None) -> dict[str, Any] | None:
    """This rule's remembered entry (for prompt injection), or ``None``."""
    if not prior_context:
        return None
    sop_rules = (prior_context.get("rule_memory") or {}).get(sop_key(rule_sop_id))
    entry = sop_rules.get(rule_key) if isinstance(sop_rules, dict) else None
    return entry if isinstance(entry, dict) else None


def build_injection_debug(cfg: EngineConfig, prior_context: dict[str, Any],
                          *, rule_key: str, rule_sop_id: Any,
                          prior_rule: dict[str, Any] | None) -> dict[str, Any]:
    """Debug record for CLAIM_MEMORY_STREAM_CONTEXT.

    Describes exactly what memory context was injected into one rule's prompt
    — or, just as importantly for debugging, WHY nothing was. Streamed on the
    ``rule_evaluated`` SSE event as ``prior_context`` and persisted to
    ``RuleEvaluation.injected_context`` when the flag is on.
    """
    if prior_rule is None:
        if not cfg.claim_memory_enabled:
            reason = "memory-disabled"
        elif not prior_context:
            reason = "no-memory"        # first run of this claim (or load failed)
        else:
            reason = "no-prior-entry"   # claim has memory, but not for this rule
        return {"injected": False, "reason": reason}

    skey = sop_key(rule_sop_id)
    payload_changed = bool(
        (prior_context.get("payload_changed_by_sop") or {}).get(skey))
    seeded = bool(prior_rule.get("seeded"))
    return {
        "injected": True,
        "sop_id": skey,
        "seeded": seeded,
        "payload_changed": payload_changed,
        # Whether prior_wins could adopt this entry on a verdict flip — false
        # for seeds (injection-only) and for SOPs whose claim data changed.
        "pin_eligible": (cfg.claim_memory_enabled
                         and cfg.claim_memory_conflict_policy == "prior_wins"
                         and not payload_changed and not seeded),
        "context": {
            "matched": bool(prior_rule.get("matched")),
            "skipped": bool(prior_rule.get("skipped")),
            "confidence": prior_rule.get("confidence"),
            "llm_status": prior_rule.get("llm_status") or "",
            "reasoning": str(prior_rule.get("reasoning") or "")[:_REASONING_CAP],
            "from_run": prior_rule.get("run_id") or "",
            "at": prior_rule.get("at") or "",
        },
    }


def make_drift_entry(*, run_id: str, scope: str, prior: dict[str, Any],
                     live: dict[str, Any], claim_data_changed: bool,
                     resolution: str, rule_key: str = "",
                     sop_id: Any = None) -> dict[str, Any]:
    entry = {
        "run_id": run_id,
        "scope": scope,
        "prior": prior,
        "live": live,
        "claim_data_changed": claim_data_changed,
        "resolution": resolution,
        "at": _now().isoformat(),
    }
    if rule_key:
        entry["rule_key"] = rule_key
    if sop_id not in (None, ""):
        entry["sop_id"] = sop_key(sop_id)
    return entry


# ── Conflict resolution (claim level backstop) ──────────────────────────────

def apply_claim_level_policy(cfg: EngineConfig, state: dict[str, Any]
                             ) -> dict[str, Any]:
    """Backstop after aggregation: if the final decision still drifted from the
    remembered one (prior_wins, unchanged data), adopt the prior decision +
    narrative + codes and record the live output in a claim-scope drift entry.

    Returns a dict of state keys to overwrite ({} = no change).
    """
    try:
        prior_context = state.get("prior_context") or {}
        if not cfg.claim_memory_enabled or not prior_context:
            return {}
        if cfg.claim_memory_conflict_policy != "prior_wins":
            return {}
        if prior_context.get("seeded_last_output"):
            # Last remembered output is a legacy import — inject-only; never
            # adopt a mapped vocabulary as the engine's decision.
            return {}
        if prior_context.get("payload_changed"):
            return {}
        status = state.get("status") or "COMPLETED"
        # TERMINATED_EARLY is excluded: a DENY/STOP halt sets the run status,
        # and adopting the prior decision here would leave status and decision
        # contradicting each other (list view DEFECT vs trace CLEAN). A halt
        # from a *remembered* rule is already pinned at rule level; a halt
        # from a brand-new rule is legitimate new behaviour, not drift.
        if status not in ("RUNNING", "COMPLETED"):
            return {}
        prior_decision = prior_context.get("last_decision_type") or ""
        live_decision = state.get("final_decision_type") or ""
        if not prior_decision or not live_decision \
                or prior_decision == live_decision:
            return {}
        drift = list(state.get("drift_entries") or [])
        drift.append(make_drift_entry(
            run_id=str(state.get("run_id") or ""),
            scope="claim",
            prior={"final_decision_type": prior_decision},
            live={"final_decision_type": live_decision,
                  "narrative": str(state.get("narrative") or "")[:_NARRATIVE_CAP],
                  "applied_codes": list(state.get("applied_codes") or [])},
            claim_data_changed=False,
            resolution="prior_wins",
        ))
        logger.info(
            "claim_memory claim=%s final decision drifted (%s -> %s); "
            "prior_wins adopted the remembered decision",
            state.get("claim_id") or "-", prior_decision, live_decision)
        return {
            "final_decision_type": prior_decision,
            "narrative": prior_context.get("last_narrative") or "",
            # Codes travel with the decision: keeping the live codes under an
            # adopted decision would persist e.g. a defect code on an ALLOW.
            "applied_codes": list(prior_context.get("last_applied_codes") or []),
            "drift_entries": drift,
        }
    except Exception:
        logger.exception("claim_memory: claim-level policy failed — keeping "
                         "live result")
        return {}


# ── Update after persist ─────────────────────────────────────────────────────

def update_claim_memory(cfg: EngineConfig, state: dict[str, Any]) -> None:
    """Rebuild the ClaimMemory row of every SOP this run touched.

    Called from n07 after the canonical rows persisted. Only successful runs
    update memory. One row per (claim_id, sop): rules grouped by their
    ``sop_id``, tool invocations by the SOP that triggered them, and the run's
    final output stamped on every touched row.
    """
    if not cfg.claim_memory_enabled:
        return
    claim_id = state.get("claim_id") or ""
    status = state.get("status") or "COMPLETED"
    if status == "RUNNING":
        status = "COMPLETED"
    if not claim_id or status not in _SUCCESS_STATUSES:
        return

    from django.db import transaction
    from execution_app.models import ClaimMemory

    now_iso = _now().isoformat()
    run_id = str(state.get("run_id") or "")
    prior_context = state.get("prior_context") or {}
    prior_rules_by_sop = prior_context.get("rule_memory") or {}
    changed_by_sop = prior_context.get("payload_changed_by_sop") or {}

    # Bindings whose tool call failed this run: a verdict that relied on them
    # reflects the outage, not the claim (e.g. an AuthenticationError turning
    # every step Inconclusive) — it must not become the pinned memory.
    failed_bindings = {
        str(bid) for bid, rec in (state.get("tool_results") or {}).items()
        if isinstance(rec, dict) and not rec.get("ok", True)
    }

    # ── group rule results per SOP ──
    rules_by_sop: dict[str, dict[str, Any]] = {}
    titles_by_sop: dict[str, str] = {}
    for ev in (state.get("rule_results") or []):
        skey = sop_key(ev.get("sop_id"))
        titles_by_sop.setdefault(skey, str(ev.get("sop_title") or ""))
        bucket = rules_by_sop.setdefault(skey, {})
        reasoning = str(ev.get("reasoning") or "")
        skip_reason = str(ev.get("skip_reason") or "")
        # Rows that carry no fresh, healthy LLM judgment must never overwrite
        # (or become) remembered verdicts:
        #   * LLM-outage fallbacks — pinning one would adopt it over every
        #     healthy future evaluation;
        #   * routing skips (goto/halt/out-of-scope) — a consequence of THIS
        #     run's path, not an evaluation of the rule (an applicable_when
        #     skip IS an LLM judgment and is kept);
        #   * verdicts that relied on a failed tool — they encode the outage,
        #     not the claim.
        # Carry the previous healthy entry forward instead — but only when the
        # claim data is unchanged for this SOP: a verdict computed against old
        # data must never be re-stamped under the new payload hash.
        is_routing_skip = bool(ev.get("skipped")) and not skip_reason.startswith(
            "not-applicable: applicable_when")
        relied_on_failed_tool = any(
            str(b) in failed_bindings
            for b in (ev.get("tool_results_used") or []))
        if (reasoning.startswith("LLM fallback") or is_routing_skip
                or relied_on_failed_tool):
            if not changed_by_sop.get(skey):
                prior_entry = (prior_rules_by_sop.get(skey) or {}).get(ev["rule_key"])
                if isinstance(prior_entry, dict):
                    bucket[ev["rule_key"]] = prior_entry
            continue
        bucket[ev["rule_key"]] = {
            "matched": bool(ev.get("matched")),
            "skipped": bool(ev.get("skipped")),
            "confidence": float(ev.get("confidence") or 0.0),
            "reasoning": reasoning[:_REASONING_CAP],
            "decision_type": ev.get("decision_type") or "",
            "llm_status": ev.get("llm_status") or "",
            "navigation": ev.get("navigation"),
            "run_id": run_id,
            "at": now_iso,
        }

    # SOPs that actually produced rule rows this run. Rows touched below only
    # to park a tool keep their remembered rule verdicts untouched.
    sops_with_rules = set(rules_by_sop)

    # ── group this run's live tool calls per SOP ──
    # Carried-forward entries keep their original called_at so the TTL is
    # never artificially extended; rows whose claim data changed rebuild from
    # live calls only.
    def _carried_tools(skey: str) -> dict[str, Any]:
        if changed_by_sop.get(skey):
            return {}
        return dict((prior_context.get("tool_memory_by_sop") or {}).get(skey) or {})

    tools_by_sop: dict[str, dict[str, Any]] = {
        skey: _carried_tools(skey) for skey in rules_by_sop
    }
    for inv in (state.get("tool_invocations") or []):
        if inv.get("phase") != "EVALUATE" or not inv.get("ok"):
            continue
        if inv.get("reused_from_run"):
            continue
        skey = sop_key(inv.get("sop_id"))
        if skey not in rules_by_sop:
            # Tool fired for a shape whose rules never evaluated; park it on
            # the shared '' row so the result is still reusable next run.
            skey = ""
            rules_by_sop.setdefault("", {})
            tools_by_sop.setdefault("", _carried_tools(""))
        result = inv.get("result")
        if not isinstance(result, (dict, list)):
            result = {"value": result}
        tools_by_sop.setdefault(skey, {})[
            tool_memory_key(inv["tool_name"], inv.get("args") or {})] = {
            "ok": True,
            "result": result,
            "phase": "EVALUATE",
            "run_id": run_id,
            "called_at": now_iso,
        }

    if not rules_by_sop:
        return

    # ── group drift entries per SOP (claim-scope goes to every touched row) ──
    drift_by_sop: dict[str, list] = {skey: [] for skey in rules_by_sop}
    for entry in (state.get("drift_entries") or []):
        if entry.get("scope") == "rule":
            drift_by_sop.setdefault(sop_key(entry.get("sop_id")), []).append(entry)
        else:
            for skey in drift_by_sop:
                drift_by_sop[skey].append(entry)

    history_entry = {
        "run_id": run_id,
        "batch_id": state.get("batch_id") or "",
        "workflow_id": str(state.get("workflow_id") or ""),
        "status": status,
        "decision_type": state.get("final_decision_type") or "",
        "codes": list(state.get("applied_codes") or []),
        "finished_at": now_iso,
    }
    current_hash = payload_hash(state.get("claim") or {})

    for skey, rule_mem in rules_by_sop.items():
        with transaction.atomic():
            mem = (ClaimMemory.objects.select_for_update()
                   .filter(claim_id=claim_id, sop_id=skey)
                   .first())
            if mem is None:
                mem = ClaimMemory(claim_id=claim_id, sop_id=skey)
            mem.sop_title = titles_by_sop.get(skey) or mem.sop_title
            mem.runs_count = (mem.runs_count or 0) + 1
            mem.last_run_id = run_id or None
            mem.last_decision_type = state.get("final_decision_type") or ""
            mem.last_narrative = str(state.get("narrative") or "")[:_NARRATIVE_CAP]
            mem.claim_payload_hash = current_hash
            if skey in sops_with_rules:
                mem.rule_memory = rule_mem
            # else: row touched only to park tools — its remembered rule
            # verdicts (written by some other run/workflow) stay intact.
            mem.tool_memory = tools_by_sop.get(skey) or {}
            mem.run_history = (list(mem.run_history or [])
                               + [history_entry])[-_RUN_HISTORY_CAP:]
            mem.drift = (list(mem.drift or [])
                         + drift_by_sop.get(skey, []))[-_DRIFT_CAP:]
            mem.save()
    logger.info(
        "claim_memory claim=%s updated %d sop row(s): %s",
        claim_id, len(rules_by_sop),
        ", ".join(f"{k or '-'}({len(v)} rules)" for k, v in rules_by_sop.items()))
