"""execute_shapes — routing-aware evaluation of a workflow's SOP rules.

Historically this iterated every Shape in canvas order and evaluated every
rule on it (one LLM call each), honoring only a DENY/STOP early halt. That
ran steps the SOP explicitly says to skip ("skip to Step 4", "proceed to
step 8 directly", "out of scope / stop auditing").

This version drives evaluation off the SOP ``step_number`` with a step cursor
that honors the routing already captured at ingestion:

  * ``goto_step``        — static "skip to step N" parsed from the SOP text.
  * ``is_out_of_scope`` / ``is_final`` — a matched rule that stops the path.
  * LLM ``navigation``   — ``{"op": "goto"|"stop"|"next", "step_number": N}``
                           returned by the per-rule evaluator (may override).
  * ``applicable``       — a rule the LLM marks not-applicable is SKIPPED, not
                           evaluated against Met/Not-Met.

Steps that are jumped over (or follow a stop) are marked SKIPPED — recorded
in ``rule_results`` with ``skipped=True`` and emitted as ``step_skipped`` SSE
events — so no LLM call is spent on them and the UI can grey them out.

Preconditions (``source == "precondition"``) carry no step routing; they are
evaluated first, in order, as gating checks (they can still DENY/STOP halt).

Routing precedence per step (after its applicable rules are evaluated):
  1. matched DENY/STOP            -> TERMINATED_EARLY (defect halt)
  2. LLM navigation op=goto       -> jump to target step
  3. matched rule static goto_step-> jump to target step
  4. LLM nav op=stop OR matched out-of-scope/is_final -> clean stop
  5. otherwise                    -> next sequential step

Forward-only jumps + a visited set + a hop cap guard against loops.
"""

from __future__ import annotations

import concurrent.futures as _cf
import contextvars
import logging
import re
import threading
import time
from typing import Any

from ..claim_fetcher import FETCH_TOOL, PARSE_TOOL
from ..config import get_config
from ..llm import publish_event
from ..state import ExecutionState
from ..tool_runner import invoke_tool
from ._eval_common import _tool_context_for_rule, evaluate_one_rule

logger = logging.getLogger(__name__)

_HALT_DECISION_TYPES = {"DENY", "STOP"}
_SKIP_TOOLS = {FETCH_TOOL, PARSE_TOOL}


class _Sink:
    """Per-cursor result collector.

    Each SOP cursor (and the precondition phase) writes into its own ``_Sink``
    so cursors can run on separate threads with no shared-list contention. The
    sinks are concatenated in canvas order afterwards and ``order_index`` is
    assigned then, so the persisted/returned ordering is deterministic
    regardless of thread completion order.
    """

    __slots__ = ("results",)

    def __init__(self) -> None:
        self.results: list[dict] = []


def _merge_args(template: dict, claim: dict) -> dict:
    """Mirror n03_run_tools: start from the template, fill obvious claim defaults."""
    args = dict(template or {})
    for key in (
        "subscriber_id",
        "member_id",
        "claim_id",
        "diagnosis_code",
        "cpt_code",
        "place_of_service",
        "first_name",
        "last_name",
        "dob",
    ):
        if key in claim and key not in args:
            args[key] = claim[key]
    # Canonical claim identifier required by several in-process tools (alias of
    # claim id); MCP-routed tools ignore the extra arg.
    if "claim_number" not in args:
        cn = claim.get("claim_number") or claim.get("claim_id")
        if cn:
            args["claim_number"] = cn
    return args


def _mk_result(
    rule: dict,
    order_index: int,
    *,
    verdict: dict | None,
    meta: dict | None,
    matched: bool,
    skipped: bool,
    skip_reason: str = "",
) -> dict:
    """Build one ``rule_results`` entry (shared by evaluated + skipped rows)."""
    verdict = verdict or {}
    meta = meta or {}
    ev_status = str(verdict.get("status") or "")
    evidence_refs = verdict.get("evidence_refs")
    evidence_refs = (
        [str(e) for e in evidence_refs] if isinstance(evidence_refs, list) else []
    )
    conditions = verdict.get("conditions")
    conditions = conditions if isinstance(conditions, list) else []
    navigation = (
        verdict.get("navigation")
        if isinstance(verdict.get("navigation"), dict)
        else None
    )
    return {
        "order_index": order_index,
        "shape_id": rule.get("shape_id", ""),
        "shape_label": rule.get("shape_label", ""),
        "rule_key": rule["key"],
        "binding_id": rule.get("binding_id", ""),
        "source": rule.get("source", "decision"),
        "condition": rule.get("condition", ""),
        "action": rule.get("action", ""),
        "matched": matched,
        "confidence": float(verdict.get("confidence") or 0.0),
        "reasoning": (skip_reason if skipped else str(verdict.get("reasoning") or "")),
        "decision_type": rule.get("decision_type", ""),
        "codes": list(rule.get("codes") or []),
        "eob_codes": list(rule.get("eob_codes") or []),
        "tool_results_used": [],
        "llm_provider": meta.get("provider", ""),
        "llm_ms": int(meta.get("ms") or 0),
        # ── additive trace fields ──
        "sop_id": rule.get("sop_id"),
        "sop_title": rule.get("sop_title", ""),
        "section_id": rule.get("section_id"),
        "section_label": rule.get("section_label", ""),
        "section_category": rule.get("section_category", ""),
        "yaml_rule_id": rule.get("yaml_rule_id", ""),
        "subrule_id": rule.get("subrule_id", ""),
        "step_question": rule.get("step_question", ""),
        "step_number": rule.get("step_number"),
        "llm_status": ev_status,
        "evidence_refs": evidence_refs,
        "conditions": conditions,
        # ── routing / skip metadata ──
        "skipped": skipped,
        "skip_reason": skip_reason,
        "applicable": (
            False
            if skipped and skip_reason.startswith("not-applicable")
            else bool(verdict.get("applicable", True))
        ),
        "navigation": navigation,
        "is_out_of_scope": bool(rule.get("is_out_of_scope")),
        "manual_oos": bool(rule.get("manual_oos")),
        "goto_step": rule.get("goto_step"),
    }


def _coerce_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


_CHOICE_RE = re.compile(
    r"\b(?:1st|2nd|3rd|4th|5th|6th|7th|8th|9th|first|second|third|fourth|fifth|"
    r"sixth|seventh)\s+choice\b",
    re.IGNORECASE,
)


def _is_choice_ladder(rules: list[dict]) -> bool:
    """True when a step's rules form a prioritized "Nth choice" ladder.

    These MUST be evaluated sequentially with shared verdicts so a matched
    higher-priority choice suppresses the lower ones (each lower choice's
    condition requires the earlier choices to be unsatisfied). Genuinely
    independent siblings (no choice vocabulary) can safely run in parallel.
    """
    hits = 0
    for r in rules:
        blob = f"{r.get('condition', '')} {r.get('action', '')}"
        if _CHOICE_RE.search(blob):
            hits += 1
            if hits >= 2:
                return True
    return False


_OON_RE = re.compile(r"\boon\b|out[ -]of[ -]network", re.IGNORECASE)


def _is_provsel_oon_deny(rule: dict) -> bool:
    """True for a provider-selection deny-choice whose match hinges on OON.

    Such a deny is only valid when OON was DERIVED from a provider-record match;
    if it rests on the literal network indicator we route to manual review
    instead of auto-denying (see the OON safeguard in ``_evaluate``)."""
    if rule.get("decision_type") not in _HALT_DECISION_TYPES:
        return False
    blob = f"{rule.get('condition', '')} {rule.get('action', '')}"
    return bool(_OON_RE.search(blob)) and bool(_CHOICE_RE.search(blob))


def _finding(rule: dict, verdict: dict) -> dict:
    """Compact record of an evaluated rule, shared as context with later rules
    in the same SOP (sequential evaluation)."""
    return {
        "key": rule.get("key", ""),
        "label": rule.get("section_label") or rule.get("step_question") or "",
        "matched": bool(verdict.get("_matched")),
        "skipped": bool(verdict.get("_skipped")),
        "decision_type": rule.get("decision_type", ""),
        # Share the factual finding (not just matched/not) so a later sibling
        # cannot contradict an established fact (e.g. "individual is billed").
        "reasoning": str(verdict.get("reasoning") or "")[:240],
    }


def execute_shapes(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    if state.get("status") == "FAILED":
        return {}

    cfg = get_config()
    # "parallel" mode runs every SOP to completion and fuses the verdict via
    # precedence in aggregate_decision; "linear" (default) short-circuits the
    # whole claim on the first SOP that hits a DENY/STOP and skips later SOPs.
    parallel = str(state.get("execution_mode") or "linear").lower() == "parallel"
    claim = state.get("claim") or {}
    tools_by_rule = state.get("tools_by_rule_key") or {}
    tools_by_shape = state.get("tools_by_shape") or {}
    # Copy so lazy per-step tool invocation can extend these without mutating
    # the inbound state in place; both are returned for the graph to merge.
    tool_results = dict(state.get("tool_results") or {})
    tool_invocations = list(state.get("tool_invocations") or [])
    lazy = bool(getattr(cfg, "lazy_tools", True))
    tools_run_for_shape: set[str] = set()
    shapes = state.get("shapes") or []
    claim_id = state.get("claim_id", "")

    terminated_at_shape_id = ""
    # LOB scoping inputs. ``lob_product`` is matched against each SOP's optional
    # ``lob_scope``; ``lob_out_of_scope`` is the whole-claim gate set in
    # load_bindings when the claim's LOB isn't in the workflow's supported set.
    claim_lob = state.get("claim_lob") or {}
    lob_product = str(claim_lob.get("product") or "").strip()
    lob_label = str(claim_lob.get("label") or "").strip()
    lob_out_of_scope = bool(state.get("lob_out_of_scope"))
    # Guards shared mutable tool state when lazy tools are evaluated concurrently
    # across SOP cursors. With the default pre-fetch (lazy off) this is never
    # contended, but keep it correct for the RULE_ENGINE_LAZY_TOOLS=1 path.
    _tool_lock = threading.Lock()

    def _rule_in_lob_scope(rule: dict) -> bool:
        """False when the rule's SOP is scoped to LOBs that exclude this claim."""
        scope = rule.get("lob_scope") or []
        if not scope or not lob_product:
            return True
        return lob_product in scope or (lob_label and lob_label in scope)

    def _ensure_tools(shape_id: str) -> None:
        """Lazily invoke the EVALUATE-phase tools bound to ``shape_id``.

        No-op when lazy mode is off (n03 pre-fetched them all), when the shape
        has no tools, or when this shape was already serviced. Skipped steps
        never reach here, so their tools are never called. Thread-safe so it is
        correct even when SOP cursors run concurrently in lazy mode.
        """
        if not lazy or not shape_id:
            return
        with _tool_lock:
            if shape_id in tools_run_for_shape:
                return
            tools_run_for_shape.add(shape_id)
            pending = list(tools_by_shape.get(shape_id) or [])
        for tb in pending:
            name = tb.get("tool_name")
            bid = tb.get("binding_id")
            if not name or name in _SKIP_TOOLS:
                continue
            with _tool_lock:
                if bid in tool_results:
                    continue
            args = _merge_args(tb.get("args_template") or {}, claim)
            out = invoke_tool(
                name, args,
                phase="EVALUATE",
                binding_id=bid or "",
                claim_id=claim_id or "",
            )
            record = {
                "binding_id": bid,
                "tool_name": name,
                "phase": "EVALUATE",
                "args": out.get("args") or args,
                "ok": out["ok"],
                "result": out["result"],
                "error": out["error"],
                "duration_ms": out["duration_ms"],
            }
            with _tool_lock:
                tool_invocations.append(record)
                tool_results[bid] = record

    if not shapes:
        logger.warning(
            "execute_shapes claim=%s zero shapes to iterate; no rule_evaluated "
            "events will fire and Anthropic will not be called for this claim",
            claim_id or "-",
        )

    # ── helpers ──────────────────────────────────────────────────────────────
    def _evaluate(rule: dict, sink: _Sink,
                  prior: list[dict] | None = None) -> dict:
        """Evaluate one rule (one LLM call), append + emit, return verdict.

        Returns the verdict dict augmented with ``_matched``/``_skipped`` flags
        the cursor uses for routing. A rule the LLM marks not-applicable is
        recorded SKIPPED instead of Met/Not-Met. ``order_index`` is a placeholder
        here (the per-sink position); it is reassigned globally at merge time.
        ``prior`` carries the verdicts of rules already evaluated earlier in the
        same SOP so this rule can honor a prioritized choice ladder.
        """
        _ensure_tools(rule.get("shape_id", ""))
        ctx, binding_ids = _tool_context_for_rule(
            rule, tools_by_rule, tools_by_shape, tool_results
        )
        verdict, meta = evaluate_one_rule(
            cfg, rule=rule, claim=claim, tool_context=ctx, stage="execute_shapes",
            prior_findings=prior,
        )

        applicable = bool(verdict.get("applicable", True))
        matched = bool(verdict.get("matched")) and applicable
        skipped = (not applicable) and bool(rule.get("applicable_when"))

        res = _mk_result(
            rule,
            len(sink.results),
            verdict=verdict,
            meta=meta,
            matched=matched,
            skipped=skipped,
            skip_reason=(
                "not-applicable: applicable_when not satisfied" if skipped else ""
            ),
        )
        res["tool_results_used"] = binding_ids

        # OON safeguard: a matched provider-selection deny-choice that hinges on
        # OON is only a confirmed defect when OON was derived via a provider-
        # record match. If the model determined OON from the literal indicator
        # (or couldn't determine it), downgrade DENY/STOP -> PEND so the claim is
        # routed to manual review instead of auto-denied (per SOP guidance).
        if matched and not skipped and _is_provsel_oon_deny(rule):
            basis = str(verdict.get("network_basis") or "").strip().lower()
            if basis != "provider_match":
                res["decision_type"] = "PEND"
                res["reasoning"] = (
                    "[auto-routed to manual review: OON not confirmed via "
                    f"provider-record match (basis={basis or 'unspecified'})] "
                    + res["reasoning"]
                )
                res["oon_unconfirmed"] = True

        sink.results.append(res)

        publish_event(
            "rule_evaluated",
            {
                "claim_id": claim_id,
                "shape_id": rule.get("shape_id", ""),
                "shape_label": rule.get("shape_label", ""),
                "rule_key": rule["key"],
                "rule_source": rule.get("source", "decision"),
                "matched": matched,
                "skipped": skipped,
                "decision_type": rule.get("decision_type", ""),
                "confidence": float(verdict.get("confidence") or 0.0),
                "reasoning": res["reasoning"],
                "codes": list(rule.get("codes") or []),
                "llm_provider": meta.get("provider", ""),
                "llm_model": meta.get("model", ""),
                "llm_ms": int(meta.get("ms") or 0),
                "llm_attempts": int(meta.get("attempts") or 1),
            },
        )
        verdict["_matched"] = matched
        verdict["_skipped"] = skipped
        return verdict

    def _mark_skipped(rules: list[dict], reason: str, step_no: Any,
                      sink: _Sink) -> None:
        """Record a SKIPPED row per rule and emit a step_skipped SSE — no LLM."""
        for rule in rules:
            sink.results.append(
                _mk_result(
                    rule,
                    len(sink.results),
                    verdict=None,
                    meta=None,
                    matched=False,
                    skipped=True,
                    skip_reason=reason,
                )
            )
        if rules:
            publish_event(
                "step_skipped",
                {
                    "claim_id": claim_id,
                    "step_number": step_no,
                    "shape_id": rules[0].get("shape_id", ""),
                    "shape_label": rules[0].get("shape_label", ""),
                    "rules_skipped": len(rules),
                    "reason": reason,
                },
            )

    # ── partition rules: preconditions (gating) vs decisions (routed) ─────────
    preconditions: list[dict] = []
    decisions: list[dict] = []
    for shape in shapes:
        for rule in shape.get("rules") or []:
            (
                preconditions if rule.get("source") == "precondition" else decisions
            ).append(rule)

    main_sink = _Sink()

    def _merge_and_finish(sop_sinks_in_order: list[_Sink]) -> dict:
        """Concatenate sinks in canvas order, renumber order_index, finish."""
        merged: list[dict] = list(main_sink.results)
        for s in sop_sinks_in_order:
            merged.extend(s.results)
        for idx, r in enumerate(merged):
            r["order_index"] = idx
        evaluated = sum(1 for r in merged if not r.get("skipped"))
        skipped_n = sum(1 for r in merged if r.get("skipped"))
        stages.append(
            {
                "node": "execute_shapes:summary",
                "status": "OK",
                "ms": int((time.time() - t0) * 1000),
                "msg": (
                    f"[{'parallel' if parallel else 'linear'}] lob={lob_label or '-'} "
                    f"{evaluated} rules evaluated, {skipped_n} skipped"
                    + (" -> TERMINATED_EARLY" if terminated_at_shape_id else "")
                ),
            }
        )
        return _finish(
            merged, stages, terminated_at_shape_id, tool_invocations, tool_results
        )

    # ── whole-claim LOB gate ──────────────────────────────────────────────────
    # The claim's Line of Business is not in this workflow's supported set, so
    # none of its rules apply: skip every rule with NO LLM call and finish as a
    # clean out-of-scope (not a defect). No-op unless ``supported_lob`` is set
    # on the workflow.
    if lob_out_of_scope:
        for rule in preconditions + decisions:
            _mark_skipped(
                [rule],
                f"out of scope: claim LOB {lob_label or lob_product} not audited by this workflow",
                rule.get("step_number"),
                main_sink,
            )
        logger.info(
            "execute_shapes claim=%s LOB %s out of scope — %d rules skipped, no LLM",
            claim_id or "-", lob_label or lob_product, len(main_sink.results),
        )
        return _merge_and_finish([])

    # ── phase 1: preconditions, in order; may DENY/STOP halt ──────────────────
    for rule in preconditions:
        # Auditor marked this node out of scope on the canvas — exclude its
        # rules from execution entirely (no LLM call), record as SKIPPED.
        if rule.get("manual_oos"):
            _mark_skipped([rule], "manually marked out of scope (excluded from execution)", None, main_sink)
            continue
        verdict = _evaluate(rule, main_sink)
        # In parallel mode a precondition match is just another contributing
        # rule (fused later by precedence) — it never halts the whole claim.
        if (
            not parallel
            and verdict["_matched"]
            and rule.get("decision_type") in _HALT_DECISION_TYPES
        ):
            terminated_at_shape_id = rule.get("shape_id", "")
            stages.append(
                {
                    "node": "execute_shapes",
                    "status": "OK",
                    "ms": int((time.time() - t0) * 1000),
                    "msg": f"precondition halt at {rule['key']}",
                }
            )
            return _merge_and_finish([])

    # ── phase 2: per-SOP step cursors ─────────────────────────────────────────
    # A single workflow may chain several SOPs (e.g. a full claim-audit pipeline
    # that runs Initial Verification → Member Eligibility → … → Coverage). Each
    # SOP numbers its own steps 1..N and routes ("skip to step 17") within that
    # local namespace, so we MUST NOT bucket decisions by a global step_number —
    # that would collide step 1 of SOP-A with step 1 of SOP-B and make
    # cross-SOP `goto` ambiguous. Instead we run one independent step cursor per
    # SOP, in canvas order (the order each SOP first appears across the
    # workbench/shape-ordered `shapes`). For a single-SOP workflow this is
    # exactly the previous behaviour.
    #
    # Routing semantics are preserved per SOP:
    #   * out-of-scope / terminal / nav-stop  → end THIS SOP, advance to the next
    #   * DENY/STOP match                      → halt the WHOLE claim (defect)
    #
    # ``sop_order`` is the de-duplicated sequence of sop_ids as decisions appear
    # in canvas order; ``decisions_by_sop`` groups the rules.
    sop_order: list[Any] = []
    seen_sops: set[Any] = set()
    decisions_by_sop: dict[Any, list[dict]] = {}
    for rule in decisions:
        sid = rule.get("sop_id")
        if sid not in seen_sops:
            seen_sops.add(sid)
            sop_order.append(sid)
        decisions_by_sop.setdefault(sid, []).append(rule)

    def _run_sop_cursor(sop_decisions: list[dict], sink: _Sink) -> tuple[str, bool, int, int]:
        """Run one SOP's step cursor. Returns
        ``(halt_shape_id, terminated_clean, visited_count, step_count)``.

        ``halt_shape_id`` is non-empty only on a DENY/STOP defect halt, which
        the caller propagates as a whole-claim TERMINATED_EARLY. Writes all
        rows into ``sink`` so cursors can run concurrently without contention.
        """
        def mark_skipped(rules, reason, step_no):
            _mark_skipped(rules, reason, step_no, sink)

        # Verdicts of rules already evaluated in THIS SOP, in evaluation order.
        # Shared into each subsequent rule's prompt so a prioritized choice
        # ladder is honored (a matched higher choice suppresses lower ones).
        # Bounded so a long SOP can't blow up the prompt.
        sop_findings: list[dict] = []

        def _prior_ctx() -> list[dict]:
            return sop_findings[-20:]

        steps_by_num: dict[int, list[dict]] = {}
        step_shape: dict[int, dict] = {}
        for rule in sop_decisions:
            sn = _coerce_int(rule.get("step_number"))
            if sn is None:
                sn = 10_000 + len(steps_by_num)  # park step-less rows at the end
            steps_by_num.setdefault(sn, []).append(rule)
            step_shape.setdefault(sn, rule)
        ordered_steps = sorted(steps_by_num.keys())

        def _index_of(step_no: int) -> int | None:
            """Index of the first ordered step >= step_no (exact preferred)."""
            for i, s in enumerate(ordered_steps):
                if s >= step_no:
                    return i
            return None

        visited: set[int] = set()
        i = 0
        hops = 0
        max_hops = len(ordered_steps) * 3 + 10
        terminate_clean = False
        halt_shape_id = ""

        while i < len(ordered_steps) and hops < max_hops:
            hops += 1
            step_no = ordered_steps[i]
            if step_no in visited:
                i += 1
                continue
            visited.add(step_no)

            rules = steps_by_num[step_no]
            shape_meta = step_shape[step_no]
            publish_event(
                "shape_start",
                {
                    "claim_id": claim_id,
                    "shape_id": shape_meta.get("shape_id", ""),
                    "shape_label": shape_meta.get("shape_label", ""),
                    "step_number": step_no,
                    "rules_total": len(rules),
                },
            )

            # Evaluate this step's rules. APPLICABLE_ONLY: once one applicable
            # sibling has matched, skip the remaining siblings with no LLM call.
            applicable_only = any(
                r.get("aggregation") == "APPLICABLE_ONLY" for r in rules
            )
            verdicts: list[tuple[dict, dict]] = []

            # Pass 1 — cheap deterministic skips (no LLM). Whatever survives goes
            # to ``to_eval`` for an actual LLM evaluation.
            to_eval: list[dict] = []
            for rule in rules:
                # Auditor manually excluded this node on the canvas: skip every
                # rule in place (no LLM call) and continue to the next step. This
                # is a pure exclusion — it never triggers the SOP "out of scope →
                # stop auditing" clean-stop, so the rest of the SOP still runs.
                if rule.get("manual_oos"):
                    mark_skipped(
                        [rule],
                        "manually marked out of scope (excluded from execution)",
                        step_no,
                    )
                    continue
                # SOP scoped to other Lines of Business than this claim's: those
                # rules are not in scope for this LOB — skip with NO LLM call.
                if not _rule_in_lob_scope(rule):
                    mark_skipped(
                        [rule],
                        f"out of scope: rule applies to LOB {rule.get('lob_scope')}, "
                        f"claim is {lob_label or lob_product}",
                        step_no,
                    )
                    continue
                # Out-of-scope steps WITHOUT an adverse code are excluded from
                # auditing: skip in place with NO LLM call. Out-of-scope rows
                # that DO carry codes (e.g. "Deny F24 ... out of scope") are
                # real findings and still evaluated. Terminal OOS exclusions
                # keep is_final=True and fall through to the clean-stop handling.
                if (rule.get("is_out_of_scope") and not rule.get("is_final")
                        and not rule.get("codes")):
                    mark_skipped(
                        [rule], "out of scope: no rule defined for this step", step_no
                    )
                    continue
                to_eval.append(rule)

            # Pass 2 — evaluate the step's rules. Two regimes:
            #  • SEQUENTIAL (top-to-bottom, shared verdicts) when the step is a
            #    prioritized "Nth choice" ladder or APPLICABLE_ONLY — a matched
            #    higher choice must suppress the lower ones, so each rule sees
            #    the verdicts of the rules already evaluated in this SOP.
            #  • PARALLEL otherwise — genuinely independent siblings have no
            #    ordering dependency, so run them concurrently for speed. They
            #    still receive the pre-step prior context (read-only).
            # SOPs themselves always run concurrently (see the cursor executor).
            sequential = applicable_only or _is_choice_ladder(to_eval)
            if sequential:
                satisfied_applicable = False
                for rule in to_eval:
                    if applicable_only and satisfied_applicable:
                        mark_skipped(
                            [rule], "not-applicable: sibling already applicable",
                            step_no,
                        )
                        continue
                    verdict = _evaluate(rule, sink, prior=_prior_ctx())
                    verdicts.append((rule, verdict))
                    sop_findings.append(_finding(rule, verdict))
                    if (applicable_only and verdict["_matched"]
                            and not verdict["_skipped"]):
                        satisfied_applicable = True
            elif len(to_eval) <= 1:
                snapshot = _prior_ctx()
                for rule in to_eval:
                    verdict = _evaluate(rule, sink, prior=snapshot)
                    verdicts.append((rule, verdict))
                    sop_findings.append(_finding(rule, verdict))
            else:
                snapshot = _prior_ctx()
                vmap: dict[int, dict] = {}

                # Worker threads open a thread-local Django connection when they
                # stamp LLMCallLog; that connection lingers for the thread's life.
                # Close it on completion so parallel batch runs don't exhaust the
                # Postgres connection pool (mirrors the SOP-cursor worker below).
                def _sibling_worker(_rule: Any) -> dict:
                    try:
                        return _evaluate(_rule, sink, snapshot)
                    finally:
                        try:
                            from django.db import connections
                            connections.close_all()
                        except Exception:  # pragma: no cover — best effort
                            pass

                with _cf.ThreadPoolExecutor(max_workers=min(len(to_eval), 8)) as sx:
                    sib_futs = {
                        sx.submit(
                            contextvars.copy_context().run,
                            _sibling_worker, rule,
                        ): rule
                        for rule in to_eval
                    }
                    for fut in _cf.as_completed(sib_futs):
                        vmap[id(sib_futs[fut])] = fut.result()
                for rule in to_eval:  # preserve canvas order
                    verdict = vmap[id(rule)]
                    verdicts.append((rule, verdict))
                    sop_findings.append(_finding(rule, verdict))

            # ── resolve routing from the matched, applicable rules ──
            matched = [
                (r, v) for (r, v) in verdicts if v["_matched"] and not v["_skipped"]
            ]

            oos_match = next(
                ((r, v) for (r, v) in matched if r.get("is_out_of_scope")), None
            )
            deny_match = next(
                (
                    (r, v)
                    for (r, v) in matched
                    if r.get("decision_type") in _HALT_DECISION_TYPES
                    and not r.get("is_out_of_scope")
                ),
                None,
            )
            final_match = any(
                r.get("is_final") and not r.get("is_out_of_scope") for (r, v) in matched
            )
            nav_goto = None
            nav_stop = False
            static_goto = None
            for r, v in matched:
                nav = (
                    v.get("navigation")
                    if isinstance(v.get("navigation"), dict)
                    else None
                )
                if nav:
                    op = str(nav.get("op") or "").lower()
                    if op == "goto" and nav_goto is None:
                        nav_goto = _coerce_int(nav.get("step_number"))
                    elif op == "stop":
                        nav_stop = True
                if static_goto is None and r.get("goto_step") is not None:
                    static_goto = _coerce_int(r.get("goto_step"))
            target = nav_goto if nav_goto is not None else static_goto

            # Precedence: out-of-scope (clean stop) > explicit forward goto >
            # DENY/STOP halt (defect) > terminal/clean stop > sequential.
            if oos_match is not None:
                terminate_clean = True
                _skip_rest(
                    ordered_steps,
                    i,
                    visited,
                    steps_by_num,
                    "skipped: prior step out of scope — auditing stopped",
                    mark_skipped,
                )
                break

            if target is not None and target > step_no:
                for s in ordered_steps[i + 1 :]:
                    if s >= target:
                        break
                    if s not in visited:
                        visited.add(s)
                        mark_skipped(
                            steps_by_num[s],
                            f"skipped: routed from step {step_no} to step {target}",
                            s,
                        )
                nxt = _index_of(target)
                if nxt is None:
                    terminate_clean = True
                    break
                i = nxt
                continue

            if deny_match is not None:
                halt_shape_id = deny_match[0].get("shape_id", "")
                _skip_rest(
                    ordered_steps,
                    i,
                    visited,
                    steps_by_num,
                    "skipped: claim halted earlier (DENY/STOP)",
                    mark_skipped,
                )
                break

            if nav_stop or final_match:
                terminate_clean = True
                _skip_rest(
                    ordered_steps,
                    i,
                    visited,
                    steps_by_num,
                    "skipped: audit path ended (terminal/stop)",
                    mark_skipped,
                )
                break

            i += 1

        if hops >= max_hops:
            logger.warning(
                "execute_shapes claim=%s sop hit max_hops=%d — routing loop guard tripped",
                claim_id or "-",
                max_hops,
            )
        return halt_shape_id, terminate_clean, len(visited), len(ordered_steps)

    # One sink per SOP so cursors never share a list.
    sop_sinks: dict[Any, _Sink] = {sid: _Sink() for sid in sop_order}

    eval_workers = max(1, min(int(getattr(cfg, "sop_eval_workers", 8) or 8),
                              len(sop_order) or 1))

    if parallel and len(sop_order) > 1 and eval_workers > 1:
        # PARALLEL: the SOPs are independent, so run their step cursors
        # concurrently. Each cursor short-circuits internally (skip/goto/stop)
        # and writes to its own sink; no SOP halts the whole claim, so the
        # verdict is fused by aggregate_decision. Propagate contextvars so
        # LLMCallLog stamping + SSE publishing keep working in worker threads,
        # and close each worker thread's DB connections when it finishes.
        def _cursor_worker(sid: Any) -> tuple[str, bool, int, int]:
            try:
                return _run_sop_cursor(decisions_by_sop.get(sid) or [], sop_sinks[sid])
            finally:
                try:
                    from django.db import connections
                    connections.close_all()
                except Exception:  # pragma: no cover — best effort
                    pass

        # Each SOP gets its OWN copied Context: a single Context cannot be
        # entered concurrently by multiple threads (RuntimeError), which would
        # serialize the cursors and block the executor on shutdown.
        with _cf.ThreadPoolExecutor(max_workers=eval_workers) as ex:
            futs = {
                ex.submit(contextvars.copy_context().run, _cursor_worker, sid): sid
                for sid in sop_order
            }
            for fut in _cf.as_completed(futs):
                fut.result()  # propagate any cursor exception
    else:
        # LINEAR (or single SOP): sequential, preserving the whole-claim halt —
        # a DENY/STOP anywhere stops the pipeline and skips any later SOPs.
        for idx, sid in enumerate(sop_order):
            halt_shape_id, _clean, _nv, _ns = _run_sop_cursor(
                decisions_by_sop.get(sid) or [], sop_sinks[sid]
            )
            if halt_shape_id and not parallel:
                terminated_at_shape_id = halt_shape_id
                for later_sid in sop_order[idx + 1 :]:
                    _mark_skipped(
                        decisions_by_sop.get(later_sid) or [],
                        "skipped: claim halted earlier (DENY/STOP)",
                        None,
                        sop_sinks[later_sid],
                    )
                break

    return _merge_and_finish([sop_sinks[sid] for sid in sop_order])


def _skip_rest(ordered_steps, i, visited, steps_by_num, reason, mark_skipped):
    """Mark every not-yet-visited step after position ``i`` as SKIPPED."""
    for s in ordered_steps[i + 1 :]:
        if s not in visited:
            visited.add(s)
            mark_skipped(steps_by_num[s], reason, s)


def _finish(
    rule_results, stages, terminated_at_shape_id, tool_invocations, tool_results
):
    out: dict = {
        "rule_results": rule_results,
        "stages": stages,
        "tool_invocations": tool_invocations,
        "tool_results": tool_results,
    }
    if terminated_at_shape_id:
        out["status"] = "TERMINATED_EARLY"
        out["terminated_at_shape_id"] = terminated_at_shape_id
    return out
