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

import logging
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
        "goto_step": rule.get("goto_step"),
    }


def _coerce_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def execute_shapes(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    if state.get("status") == "FAILED":
        return {}

    cfg = get_config()
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

    rule_results: list[dict] = []
    order_box = [0]  # mutable counter shared by the helpers below
    terminated_at_shape_id = ""

    def _ensure_tools(shape_id: str) -> None:
        """Lazily invoke the EVALUATE-phase tools bound to ``shape_id``.

        No-op when lazy mode is off (n03 already ran them), when the shape has
        no tools, or when this shape was already serviced. Skipped steps never
        reach here, so their tools are never called.
        """
        if not lazy or not shape_id or shape_id in tools_run_for_shape:
            return
        tools_run_for_shape.add(shape_id)
        for tb in tools_by_shape.get(shape_id) or []:
            name = tb.get("tool_name")
            bid = tb.get("binding_id")
            if not name or name in _SKIP_TOOLS or bid in tool_results:
                continue
            args = _merge_args(tb.get("args_template") or {}, claim)
            out = invoke_tool(name, args)
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
            tool_invocations.append(record)
            tool_results[bid] = record

    if not shapes:
        logger.warning(
            "execute_shapes claim=%s zero shapes to iterate; no rule_evaluated "
            "events will fire and Anthropic will not be called for this claim",
            claim_id or "-",
        )

    # ── helpers ──────────────────────────────────────────────────────────────
    def _evaluate(rule: dict) -> dict:
        """Evaluate one rule (one LLM call), append + emit, return verdict.

        Returns the verdict dict augmented with ``_matched``/``_skipped`` flags
        the cursor uses for routing. A rule the LLM marks not-applicable is
        recorded SKIPPED instead of Met/Not-Met.
        """
        _ensure_tools(rule.get("shape_id", ""))
        ctx, binding_ids = _tool_context_for_rule(
            rule, tools_by_rule, tools_by_shape, tool_results
        )
        verdict, meta = evaluate_one_rule(
            cfg, rule=rule, claim=claim, tool_context=ctx, stage="execute_shapes"
        )

        applicable = bool(verdict.get("applicable", True))
        matched = bool(verdict.get("matched")) and applicable
        skipped = (not applicable) and bool(rule.get("applicable_when"))

        res = _mk_result(
            rule,
            order_box[0],
            verdict=verdict,
            meta=meta,
            matched=matched,
            skipped=skipped,
            skip_reason=(
                "not-applicable: applicable_when not satisfied" if skipped else ""
            ),
        )
        res["tool_results_used"] = binding_ids
        rule_results.append(res)

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
        order_box[0] += 1
        verdict["_matched"] = matched
        verdict["_skipped"] = skipped
        return verdict

    def _mark_skipped(rules: list[dict], reason: str, step_no: Any) -> None:
        """Record a SKIPPED row per rule and emit a step_skipped SSE — no LLM."""
        for rule in rules:
            rule_results.append(
                _mk_result(
                    rule,
                    order_box[0],
                    verdict=None,
                    meta=None,
                    matched=False,
                    skipped=True,
                    skip_reason=reason,
                )
            )
            order_box[0] += 1
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

    # ── phase 1: preconditions, in order; may DENY/STOP halt ──────────────────
    for rule in preconditions:
        verdict = _evaluate(rule)
        if verdict["_matched"] and rule.get("decision_type") in _HALT_DECISION_TYPES:
            terminated_at_shape_id = rule.get("shape_id", "")
            stages.append(
                {
                    "node": "execute_shapes",
                    "status": "OK",
                    "ms": int((time.time() - t0) * 1000),
                    "msg": f"precondition halt at {rule['key']}",
                }
            )
            return _finish(
                rule_results,
                stages,
                terminated_at_shape_id,
                tool_invocations,
                tool_results,
            )

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

    def _run_sop_cursor(sop_decisions: list[dict]) -> tuple[str, bool, int, int]:
        """Run one SOP's step cursor. Returns
        ``(halt_shape_id, terminated_clean, visited_count, step_count)``.

        ``halt_shape_id`` is non-empty only on a DENY/STOP defect halt, which
        the caller propagates as a whole-claim TERMINATED_EARLY.
        """
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
            satisfied_applicable = False
            for rule in rules:
                # Blank out-of-scope steps (no rule defined — flagged OOS and
                # non-final by the importer) are excluded from auditing: skip in
                # place with NO LLM call and continue to the next step. Terminal
                # OOS exclusions keep is_final=True and fall through to the
                # match→clean-stop handling below.
                if rule.get("is_out_of_scope") and not rule.get("is_final"):
                    _mark_skipped(
                        [rule], "out of scope: no rule defined for this step", step_no
                    )
                    continue
                if applicable_only and satisfied_applicable:
                    _mark_skipped(
                        [rule], "not-applicable: sibling already applicable", step_no
                    )
                    continue
                verdict = _evaluate(rule)
                verdicts.append((rule, verdict))
                if applicable_only and verdict["_matched"] and not verdict["_skipped"]:
                    satisfied_applicable = True

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
                    _mark_skipped,
                )
                break

            if target is not None and target > step_no:
                for s in ordered_steps[i + 1 :]:
                    if s >= target:
                        break
                    if s not in visited:
                        visited.add(s)
                        _mark_skipped(
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
                    _mark_skipped,
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
                    _mark_skipped,
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

    any_clean_stop = False
    total_visited = 0
    total_steps = 0
    for sid in sop_order:
        halt_shape_id, clean, n_visited, n_steps = _run_sop_cursor(
            decisions_by_sop.get(sid) or []
        )
        total_visited += n_visited
        total_steps += n_steps
        any_clean_stop = any_clean_stop or clean
        if halt_shape_id:
            # A DENY/STOP anywhere is a whole-claim defect halt: stop the
            # pipeline and skip any later SOPs that have not run yet.
            terminated_at_shape_id = halt_shape_id
            for later_sid in sop_order[sop_order.index(sid) + 1 :]:
                _mark_skipped(
                    decisions_by_sop.get(later_sid) or [],
                    "skipped: claim halted earlier (DENY/STOP)",
                    None,
                )
            break

    evaluated = sum(1 for r in rule_results if not r.get("skipped"))
    skipped_n = sum(1 for r in rule_results if r.get("skipped"))
    stages.append(
        {
            "node": "execute_shapes:summary",
            "status": "OK",
            "ms": int((time.time() - t0) * 1000),
            "msg": (
                f"{evaluated} rules evaluated, {skipped_n} skipped across "
                f"{total_visited} of {total_steps} steps / {len(sop_order)} sop(s)"
                + (" -> TERMINATED_EARLY" if terminated_at_shape_id else "")
                + (" -> stopped (out of scope)" if any_clean_stop else "")
            ),
        }
    )
    return _finish(
        rule_results, stages, terminated_at_shape_id, tool_invocations, tool_results
    )


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
