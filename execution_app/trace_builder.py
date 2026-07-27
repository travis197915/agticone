"""Build trace.json / explainability.json-shaped audit logs from a run.

Purely additive: consumes the per-rule ``rule_results`` and ``tool_invocations``
that the engine already produces (plus the additive trace fields threaded
through ``n_execute_shapes``) and projects them into two denormalized arrays:

* ``trace``          — one entry per (agent/shape, SOP, step), each carrying
                       status, rationale, evidence_refs, subrule_results and
                       the tools used/succeeded/failed/skipped.
* ``explainability`` — the same data grouped per (agent, SOP) with light
                       step summaries.

Nothing here mutates engine state or existing rows; the output is stored in the
``ClaimTrace`` table and served by the new read endpoints.
"""

from __future__ import annotations

from typing import Any

# ── Two-layer status model ──────────────────────────────────────────────────
# RULE / STEP level — the verdict for a single SOP rule/subrule, shown on each
# trace step in the claim detail. These stay in the auditor's native vocabulary:
MET = "Met"
NOT_MET = "Not-Met"
INCONCLUSIVE_RULE = "Inconclusive"
# A rule/step the SOP routed past (goto / out-of-scope) or that was not
# applicable. Skipped entries are excluded from the claim rollup — they are
# neither a pass nor a defect — and the UI greys them out.
SKIPPED_RULE = "Skipped"
#
# AGENT / CLAIM level — the rolled-up audit outcome shown on agent chips and the
# claim header. A Met rule rolls up to CLEAN, a Not-Met rule to DEFECT:
CLEAN = "CLEAN"
DEFECT = "DEFECT"
INCONCLUSIVE = "INCONCLUSIVE"
# Scope outcomes for an agent/SOP whose steps never executed against this claim.
# These replace the old catch-all INCONCLUSIVE chip at the SOP/agent level:
#   NOT_APPLICABLE — the SOP/step did not apply (condition gate not satisfied,
#                    routed past, or nothing to evaluate).
#   OUT_OF_SCOPE   — the SOP/step was explicitly out of scope for this claim.
# Both are non-findings and are excluded from the claim-level rollup, exactly
# like a skipped step.
NOT_APPLICABLE = "NOT_APPLICABLE"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
# Engine still evaluating rules for this claim (``RuleExecutionRun.status == RUNNING``).
IN_PROGRESS = "IN_PROGRESS"

# Engine decision types that always denote a claim-handling defect / a clean pass.
_DEFECT_DECISIONS = {"DENY", "STOP", "REFER", "REFERRAL", "PEND", "PENDED"}
_CLEAN_DECISIONS = {"ALLOW", "APPROVE", "APPROVED", "PAY", "PASS"}
_HALT_DECISION_TYPES = _DEFECT_DECISIONS

# Coverage-validation tools. Per the auditor spec sheet, "Coverage validation
# failed due to ... failed tool execution" is a DEFECT for the CoverageBenefit
# process — so a *failed* coverage tool is treated as a hard finding, not as a
# silent pass. Every other tool that a step relied on, when it fails, leaves the
# auditor unable to conclude → INCONCLUSIVE (never auto-CLEAN).
_COVERAGE_TOOLS = {
    "cbd_coverage",
    "check_medicare_coverage",
    "check_coverage_commercial",
    "check_coverage_medicaid",
}


def tool_failure_status(failed_tool_names) -> str:
    """Audit impact of the tools a step relied on having failed.

    Think like a human auditor: if the coverage API you needed errored out you
    cannot sign the claim off — that is a DEFECT (Not-Met). If any other check
    you needed errored out you simply cannot conclude — that is INCONCLUSIVE.
    Returns ``""`` when nothing failed.
    """
    failed = {str(n) for n in (failed_tool_names or [])}
    if not failed:
        return ""
    if failed & _COVERAGE_TOOLS:
        return NOT_MET
    return INCONCLUSIVE_RULE


_MET_TOKENS = {"met", "match", "matched", "pass", "passed", "clean", "allow", "ok"}
_NOT_MET_TOKENS = {
    "not-met",
    "notmet",
    "fail",
    "failed",
    "deny",
    "denied",
    "defect",
    "stop",
    "refer",
    "referral",
    "pend",
    "pended",
}
_INCONCLUSIVE_TOKENS = {"inconclusive", "unknown", "indeterminate", "n/a", "na"}


def _iso(dt) -> str:
    if dt is None:
        return ""
    try:
        return dt.replace(microsecond=0).isoformat()
    except Exception:  # pragma: no cover - defensive
        return str(dt)


# ── Rule / step level ────────────────────────────────────────────────────────
def _normalize_rule_status(raw: str) -> str:
    """Map a free-form status string onto Met / Not-Met / Inconclusive.

    Returns ``""`` when empty/unrecognized so callers can fall back to other
    signals. Accepts both rule words (met) and audit words (clean/defect).
    """
    s = (raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    if not s:
        return ""
    if s in ("skipped", "skip"):
        return SKIPPED_RULE
    if s in _MET_TOKENS:
        return MET
    if s in _NOT_MET_TOKENS:
        return NOT_MET
    if s in _INCONCLUSIVE_TOKENS:
        return INCONCLUSIVE_RULE
    return ""


_INCONCLUSIVE_VERDICT_TOKENS = {"inconclusive"}


def _eval_is_inconclusive(ev: dict[str, Any]) -> bool:
    """True ONLY for a non-skipped rule carrying the deterministic ``verdict``
    ``INCONCLUSIVE`` (set by the auditor fix scripts, e.g. a Timely-Filing
    Step-10 genuine match-in-history, or a duplicate/E51 line with no history).

    Such a rule needs manual auditor review, so it must surface as INCONCLUSIVE
    at the step/agent level rather than silently rolling up to CLEAN.

    Deliberately strict: it looks ONLY at the persisted ``verdict`` field and
    ONLY for the exact token ``inconclusive``. It intentionally IGNORES
    ``llm_status`` and softer tokens (``unknown``/``indeterminate``) — those are
    noisy per-rule LLM sub-statuses present on ordinary clean rules, and using
    them would wrongly promote entire clean steps/claims to INCONCLUSIVE.
    """
    if ev.get("skipped"):
        return False
    v = (ev.get("verdict") or "").strip().lower().replace("_", "-").replace(" ", "-")
    return v in _INCONCLUSIVE_VERDICT_TOKENS


def _status_for_eval(ev: dict[str, Any]) -> str:
    """Rule-level verdict for one evaluation (Met / Not-Met / Inconclusive / Skipped)."""
    if ev.get("skipped"):
        return SKIPPED_RULE
    # An out-of-scope match is usually a clean line-item exclusion / handoff
    # ("corrected/void claim — refer to Attachment Validation, out of scope"),
    # which must not be painted Not-Met (the UI renders that as DEFECT). The
    # exception is a terminal adverse disposition that applies a code and then
    # stops (e.g. "Deny with F24 ... out of scope"): those carry codes and stay
    # real findings.
    if ev.get("is_out_of_scope") and not ev.get("codes"):
        return SKIPPED_RULE
    explicit = _normalize_rule_status(ev.get("llm_status", ""))
    if explicit:
        return explicit
    # An explicit INCONCLUSIVE verdict outranks the matched→Met default: a rule
    # attested inconclusive must never read as a clean pass.
    if _eval_is_inconclusive(ev):
        return INCONCLUSIVE_RULE
    matched = bool(ev.get("matched"))
    decision_type = (ev.get("decision_type") or "").upper()
    if matched and decision_type in _HALT_DECISION_TYPES:
        return NOT_MET
    if matched:
        return MET
    return INCONCLUSIVE_RULE


def _eval_applies_defect(ev: dict[str, Any]) -> bool:
    """True only when an evaluation *applies* an adverse disposition.

    A defect is a rule that fired (matched) AND either carries an adverse
    decision type (DENY / REFER / PEND / STOP) OR references an EOB code (per
    the verdict policy, a matched rule that references an EOB code is a defect).
    A rule whose condition is merely "Not-Met", or that routes the flow
    ("proceed" / "skip to" → CONDITIONAL), is NOT a defect — that is normal SOP
    branching. This mirrors the engine's own aggregator.
    """
    if ev.get("skipped"):
        return False
    if not ev.get("matched"):
        return False
    if ev.get("eob_codes"):
        return True
    return (ev.get("decision_type") or "").upper() in _DEFECT_DECISIONS


def _step_audit_status(evs: list[dict[str, Any]]) -> str:
    """Step verdict for the audit view (Met / Not-Met / Inconclusive / Skipped).

    Disposition-driven, NOT condition-driven: a step is Not-Met (DEFECT) only
    when one of its rules applied an adverse disposition. A step that was
    evaluated without any adverse disposition is Met (CLEAN) even if individual
    sub-checks reported "Not-Met" (e.g. "this defect code is absent" / "rule
    does not apply"). Steps with nothing to evaluate roll up to Skipped.
    """
    considered = [
        e
        for e in evs
        if not e.get("skipped")
        and not (e.get("is_out_of_scope") and not e.get("codes"))
    ]
    if not considered:
        return SKIPPED_RULE
    if any(_eval_applies_defect(e) for e in considered):
        return NOT_MET
    # A rule explicitly attested INCONCLUSIVE (e.g. Step-10 match-in-history that
    # must be manually reviewed) makes the whole step inconclusive — it is not a
    # clean pass even though it matched without an adverse disposition.
    if any(_eval_is_inconclusive(e) for e in considered):
        return INCONCLUSIVE_RULE
    evaluated = any(
        e.get("matched") or _normalize_rule_status(e.get("llm_status", ""))
        for e in considered
    )
    return MET if evaluated else INCONCLUSIVE_RULE


def _aggregate_rule_status(statuses: list[str]) -> str:
    """Roll subrule verdicts up to a step verdict (rule vocabulary).

    Skipped subrules are dropped first; a step whose rules were all skipped
    rolls up to Skipped (not Inconclusive) so the claim rollup ignores it.
    """
    norm = [_normalize_rule_status(s) or INCONCLUSIVE_RULE for s in (statuses or [])]
    norm = [s for s in norm if s != SKIPPED_RULE]
    if not norm:
        return SKIPPED_RULE
    if any(s == NOT_MET for s in norm):
        return NOT_MET
    if all(s == MET for s in norm):
        return MET
    return INCONCLUSIVE_RULE


def scope_category(skip_reason: str) -> str:
    """Classify a skipped row's ``skip_reason`` as OUT_OF_SCOPE or NOT_APPLICABLE.

    The engine writes structured ``skip_reason`` prefixes: ``out of scope: …``,
    ``not-applicable: …`` and ``skipped: …`` (routed past / terminal). Only the
    explicit out-of-scope reason maps to OUT_OF_SCOPE; everything else a step was
    skipped for is treated as NOT_APPLICABLE.
    """
    s = (skip_reason or "").strip().lower()
    if "out of scope" in s or "out-of-scope" in s:
        return OUT_OF_SCOPE
    return NOT_APPLICABLE


def node_audit_status(evals: list[dict[str, Any]]) -> str:
    """SOP/agent-level status: DEFECT | INCONCLUSIVE | CLEAN | OUT_OF_SCOPE | NOT_APPLICABLE.

    Precedence, from the persisted rule rows (no re-run needed):
      1. DEFECT         — an evaluated (non-skipped) rule applied an adverse
                          disposition (DENY/STOP/REFER/PEND) or referenced an EOB
                          code → the SOP was not handled cleanly.
      1.5 INCONCLUSIVE  — an evaluated rule is explicitly attested INCONCLUSIVE
                          (verdict/llm_status) → must be manually reviewed; it is
                          neither a clean pass nor a defect.
      2. CLEAN          — at least one rule actually executed against this claim
                          (non-skipped, not a code-less out-of-scope exclusion)
                          with no adverse finding → steps ran per SOP.
      3. OUT_OF_SCOPE   — every row was skipped and at least one was skipped as
                          explicitly out of scope.
      4. NOT_APPLICABLE — every row was skipped/routed-past for any other reason,
                          or there were no rules at all.
    """
    evals = list(evals or [])
    if not evals:
        return NOT_APPLICABLE
    if any(_eval_applies_defect(e) for e in evals):
        return DEFECT
    # An evaluated (non-skipped) rule attested INCONCLUSIVE (verdict/llm_status)
    # routes the whole node to INCONCLUSIVE — a match-in-history / no-history
    # duplicate that must be manually reviewed is neither CLEAN nor a DEFECT.
    if any(_eval_is_inconclusive(e) for e in evals):
        return INCONCLUSIVE
    executed = [
        e
        for e in evals
        if not e.get("skipped")
        and not (e.get("is_out_of_scope") and not e.get("codes"))
    ]
    if executed:
        return CLEAN
    if any(scope_category(e.get("skip_reason", "")) == OUT_OF_SCOPE for e in evals):
        return OUT_OF_SCOPE
    return NOT_APPLICABLE


# ── Agent / claim level ──────────────────────────────────────────────────────
def rule_to_audit(status: str) -> str:
    """Map a rule/agent verdict to the claim-level audit model.

    Met→CLEAN, Not-Met→DEFECT. Skipped and the SOP/agent scope states
    (NOT_APPLICABLE / OUT_OF_SCOPE) are non-findings and return ``""`` so they
    drop out of the claim-level rollup entirely.
    """
    key = (status or "").strip().upper().replace("-", "_").replace(" ", "_")
    if key in ("SKIPPED", "SKIP", "NOT_APPLICABLE", "NA", "OUT_OF_SCOPE"):
        return ""  # ignored at the claim level
    s = _normalize_rule_status(status)
    if s == MET:
        return CLEAN
    if s == NOT_MET:
        return DEFECT
    if s == SKIPPED_RULE:
        return ""  # ignored at the claim level
    return INCONCLUSIVE


def normalize_status(raw: str) -> str:
    """Public: normalize any status/decision-ish string to the audit model."""
    return rule_to_audit(raw)


def normalize_decision(decision_type: str) -> str:
    """Map an engine decision type to CLEAN / DEFECT / INCONCLUSIVE, or ``""``.

    Returns ``""`` only for genuinely unknown types so callers can fall back to
    the per-step trace rollup. The explicit ``INCONCLUSIVE`` verdict (emitted by
    the aggregator when a required tool failed) maps straight through.
    """
    d = (decision_type or "").strip().upper()
    if d in _DEFECT_DECISIONS:
        return DEFECT
    if d in _CLEAN_DECISIONS:
        return CLEAN
    if d == "INCONCLUSIVE":
        return INCONCLUSIVE
    return ""


def aggregate_status(statuses: list[str]) -> str:
    """Public: roll any list of statuses up to one audit value.

    any DEFECT → DEFECT; all CLEAN → CLEAN; otherwise INCONCLUSIVE.
    Skipped statuses map to "" and are dropped before rolling up.
    """
    audit = [a for a in (rule_to_audit(s) for s in (statuses or [])) if a]
    if not audit:
        return INCONCLUSIVE
    if any(s == DEFECT for s in audit):
        return DEFECT
    if all(s == CLEAN for s in audit):
        return CLEAN
    return INCONCLUSIVE


def claim_status(trace_list: list[dict[str, Any]]) -> str:
    """Overall audit status for a claim from its built ``trace`` array.

    The per-step ``status`` is rule-level (Met/Not-Met/Inconclusive); this rolls
    those up to the claim-level audit value (CLEAN/DEFECT/INCONCLUSIVE).
    """
    return aggregate_status([str(t.get("status") or "") for t in (trace_list or [])])


# Plain-language verbs per sub-rule verdict. Auditors asked that the agent
# *explicitly state* each checklist item's result (e.g. "Received date is
# correct in Facets per SOP") rather than leaving it implicit behind a raw
# subrule id. ``subrule_label`` + ``subrule_statement`` produce exactly that.
_STATEMENT_VERB = {
    MET: "Verified",
    NOT_MET: "Discrepancy found",
    INCONCLUSIVE_RULE: "Could not be verified",
    SKIPPED_RULE: "Not applicable",
}


def subrule_label(ev_or_condition: Any) -> str:
    """Human-readable name for a checklist sub-rule.

    Rule conditions are authored as ``"<Item Name> AND <predicate>"`` (e.g.
    ``"Receive Date (Julian Date) AND Received Date ... must match ..."``), so
    the item name is the text before the first `` AND ``. Falls back to the part
    before `` from `` and finally to a trimmed slice, so the auditor sees
    "Receive Date (Julian Date)" instead of "RULE-001-003".
    """
    if isinstance(ev_or_condition, dict):
        cond = str(ev_or_condition.get("condition") or "")
        fallback = str(
            ev_or_condition.get("subrule_id") or ev_or_condition.get("rule_key") or ""
        )
    else:
        cond = str(ev_or_condition or "")
        fallback = ""
    cond = cond.strip()
    for sep in (" AND ", " from ", " must "):
        idx = cond.find(sep)
        if idx > 0:
            return cond[:idx].strip(" .:-")
    if cond:
        return cond[:60].strip(" .:-") + ("…" if len(cond) > 60 else "")
    return fallback


def _last_condition_note(conditions: list[dict[str, Any]]) -> str:
    """Most conclusive human note across a sub-rule's conditions.

    The final condition usually carries the comparison conclusion (e.g. "exact
    match confirmed, no discrepancy"), which is the phrase auditors want to see.
    """
    note = ""
    for c in conditions or []:
        vals = c.get("values") if isinstance(c, dict) else None
        if isinstance(vals, dict):
            n = vals.get("notes")
            if isinstance(n, str) and n.strip():
                note = n.strip()
    return note


def subrule_statement(
    label: str,
    status: str,
    conditions: list[dict[str, Any]],
    reasoning: str = "",
) -> str:
    """One-line, plain-language statement of the sub-rule's outcome per SOP."""
    verb = _STATEMENT_VERB.get(status, "Reviewed")
    detail = _last_condition_note(conditions)
    if not detail:
        first = (reasoning or "").strip().split(". ")[0].strip()
        detail = first[:200]
    base = f"{label}: {verb} per SOP"
    return f"{base} — {detail}" if detail else f"{base}."


def _subrule_entry(ev: dict[str, Any]) -> dict[str, Any]:
    conditions = ev.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        conditions = [
            {
                "condition": ev.get("condition", ""),
                "evaluated": bool(ev.get("matched")),
                "using_fields": [],
                "values": {},
            }
        ]
    status = _status_for_eval(ev)
    label = subrule_label(ev)
    return {
        "subrule_id": ev.get("subrule_id") or ev.get("rule_key", ""),
        "label": label,
        "status": status,
        "statement": subrule_statement(
            label,
            status,
            conditions,
            str(ev.get("reasoning") or ""),
        ),
        "conditions": conditions,
    }


def _tool_lookup(tool_invocations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """binding_id -> {tool_name, ok}. Falls back to tool_name keys too."""
    out: dict[str, dict[str, Any]] = {}
    for inv in tool_invocations or []:
        rec = {
            "tool_name": inv.get("tool_name", ""),
            "ok": bool(inv.get("ok", True)),
            "skipped": bool(inv.get("skipped")),
        }
        bid = inv.get("binding_id") or inv.get("tool_binding_id")
        if bid:
            out[str(bid)] = rec
        if inv.get("tool_name"):
            out.setdefault(inv["tool_name"], rec)
    return out


def _group_key(ev: dict[str, Any]) -> tuple[str, Any, Any]:
    """Group leaf evaluations into one step: (shape, sop, yaml_rule_id|step)."""
    step_key = ev.get("yaml_rule_id") or ev.get("section_id") or ev.get("rule_key")
    return (str(ev.get("shape_id", "")), ev.get("sop_id"), step_key)


def build_trace(
    run,
    rule_results: list[dict[str, Any]],
    tool_invocations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(trace_list, explainability_list)`` for one claim run."""
    rule_results = rule_results or []
    tool_invocations = tool_invocations or []
    tool_by_binding = _tool_lookup(tool_invocations)

    execution_id = str(run.id)
    claim_id = run.claim_id or ""
    started_at = _iso(run.started_at)
    ended_at = _iso(run.finished_at)
    ts = ended_at or started_at

    # Preserve first-seen order of (shape, sop, step) groups.
    groups: dict[tuple[str, Any, Any], list[dict[str, Any]]] = {}
    for ev in rule_results:
        groups.setdefault(_group_key(ev), []).append(ev)

    trace: list[dict[str, Any]] = []
    for (shape_id, sop_id, step_key), evs in groups.items():
        head = evs[0]
        # A "parent" evaluation is one without a subrule_id; subrules carry one.
        parents = [e for e in evs if not e.get("subrule_id")]
        subrules = [e for e in evs if e.get("subrule_id")]
        parent = parents[0] if parents else head

        # Sub-rule rows keep their *factual* Met/Not-Met for the detail view…
        sub_results = [_subrule_entry(e) for e in subrules]
        # …but the step's audit verdict is disposition-driven: Not-Met (DEFECT)
        # only when a rule actually applied an adverse disposition, never just
        # because a sub-check's condition was Not-Met.
        step_status = _step_audit_status(evs)

        # Evidence refs: merge any LLM-supplied refs across the group.
        evidence_refs: list[str] = []
        for e in evs:
            for ref in e.get("evidence_refs") or []:
                if ref not in evidence_refs:
                    evidence_refs.append(ref)

        # Tools: resolve from the binding ids each evaluation relied on.
        used: list[str] = []
        succeeded: list[str] = []
        failed: list[str] = []
        skipped_tools: list[str] = []
        for e in evs:
            for bid in e.get("tool_results_used") or []:
                rec = tool_by_binding.get(str(bid))
                if not rec:
                    continue
                name = rec["tool_name"]
                if not name:
                    continue
                # LOB-gated (not-invoked) tool → show as skipped, not used, and
                # never let it count toward tool-failure escalation below.
                if rec.get("skipped"):
                    if name not in skipped_tools:
                        skipped_tools.append(name)
                    continue
                if name not in used:
                    used.append(name)
                    (succeeded if rec["ok"] else failed).append(name)
        for name in used:
            evidence_refs.append(
                f"TOOL_OK:{name}" if name in succeeded else f"TOOL_FAIL:{name}"
            )

        # Auditor rule: a step that relied on a tool which *failed* cannot be a
        # silent pass. A failed coverage tool is a DEFECT (Not-Met); any other
        # failed tool the step needed makes the step INCONCLUSIVE. We only
        # escalate (never downgrade a real Not-Met to Inconclusive).
        fail_status = tool_failure_status(failed)
        if fail_status == NOT_MET:
            step_status = NOT_MET
        elif fail_status == INCONCLUSIVE_RULE and step_status not in (NOT_MET,):
            step_status = INCONCLUSIVE_RULE

        llm_ms = sum(int(e.get("llm_ms") or 0) for e in evs)
        rationale = "; ".join(
            (e.get("reasoning") or "").strip()
            for e in evs
            if (e.get("reasoning") or "").strip()
        )

        trace.append(
            {
                "timestamp": ts,
                "claim_id": claim_id,
                "execution_id": execution_id,
                "agent_name": head.get("shape_label") or shape_id,
                "shape_id": shape_id,
                "sop_name": head.get("sop_title", ""),
                "sop_step_number": head.get("section_id"),
                "sop_step_name": head.get("yaml_rule_id")
                or head.get("section_label", ""),
                "sop_rule_id": head.get("yaml_rule_id", ""),
                "sop_step_description": head.get("step_question")
                or head.get("section_label", ""),
                "sop_action": parent.get("action", ""),
                "step_exec_status": "success",
                "status": step_status,
                "rationale": rationale,
                "evidence_refs": evidence_refs,
                "started_at": started_at,
                "ended_at": ended_at,
                "transaction_time_sec": round(llm_ms / 1000.0, 3),
                "tools_used": used,
                "tools_succeeded": succeeded,
                "tools_failed": failed,
                "tools_skipped": skipped_tools,
                "decision_type": parent.get("decision_type", ""),
                "codes": list(parent.get("codes") or []),
                "subrule_results": sub_results,
            }
        )

    explainability = _build_explainability(
        trace,
        execution_id,
        claim_id,
        started_at,
        ended_at,
        run,
    )
    return trace, explainability


def _build_explainability(
    trace: list[dict[str, Any]],
    execution_id: str,
    claim_id: str,
    started_at: str,
    ended_at: str,
    run,
) -> list[dict[str, Any]]:
    """Group trace steps per (agent, SOP) into the explainability shape."""
    agents: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in trace:
        key = (entry["agent_name"], entry["sop_name"])
        bucket = agents.setdefault(
            key,
            {
                "claim_id": claim_id,
                "execution_id": execution_id,
                "agent_name": entry["agent_name"],
                "sop_name": entry["sop_name"],
                "started_at": started_at,
                "ended_at": ended_at,
                "_steps": [],
            },
        )
        bucket["_steps"].append(entry)

    out: list[dict[str, Any]] = []
    for bucket in agents.values():
        steps = bucket.pop("_steps")
        statuses = [s["status"] for s in steps]
        # Agent final_status is the rolled-up audit value (CLEAN/DEFECT/INCONCLUSIVE).
        final_status = aggregate_status(statuses)
        first_failing = next(
            (s for s in steps if s["status"] not in (MET, SKIPPED_RULE)), None
        )
        rationale_summary = [f"Overall outcome: {final_status}."]
        if first_failing is not None and final_status != CLEAN:
            rationale_summary.append(
                f"First non-Met step: {first_failing.get('sop_step_name') or first_failing.get('sop_step_number')}"
                f" ({first_failing.get('sop_step_description', '')})"
            )
        total_sec = sum(float(s.get("transaction_time_sec") or 0.0) for s in steps)
        out.append(
            {
                **bucket,
                "sop_step_summary": [
                    f"SOP executed for agent '{bucket['agent_name']}' with outcome {final_status}."
                ],
                "sop_action_summary": ["Actions were executed per SOP steps."],
                "rationale_summary": rationale_summary,
                "duration": round(total_sec, 1),
                "final_status": final_status,
                "step_results": {
                    "steps": [
                        {
                            "sop_step_number": s.get("sop_step_number"),
                            "sop_step_name": s.get("sop_step_name"),
                            "sop_rule_id": s.get("sop_rule_id"),
                            "status": s.get("status"),
                            "step_exec_status": s.get("step_exec_status"),
                            "subrule_results": s.get("subrule_results", []),
                            "evidence_refs": s.get("evidence_refs", []),
                            "timestamp": s.get("timestamp"),
                            "result_summary": _result_summary(s),
                            "sop_step_description": s.get("sop_step_description"),
                            "sop_action": s.get("sop_action"),
                            "rationale": s.get("rationale"),
                            "started_at": s.get("started_at"),
                            "ended_at": s.get("ended_at"),
                            "transaction_time_sec": s.get("transaction_time_sec"),
                        }
                        for s in steps
                    ],
                },
                "router_errors": [],
            }
        )
    return out


def _result_summary(step: dict[str, Any]) -> str:
    tools = ", ".join(step.get("tools_used") or [])
    desc = step.get("sop_step_description", "")
    base = f"{step.get('status')}: {desc}"
    return f"{base} | tools: {tools}" if tools else base
