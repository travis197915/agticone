"""Shared rule-evaluation helpers for nodes 4 and 5.

Each rule produces one LLM call. The prompt gets:
* the parsed claim
* the rule's condition + action + decision_type
* any tool results scoped to that rule (via tools_by_rule_key) or to its
  shape (via tools_by_shape).
"""
from __future__ import annotations

import json
from typing import Any

from ..config import EngineConfig
from ..field_mapping import format_mapped_fields_block, resolve_sop_fields
from ..llm import llm_call

_EVAL_REQUIRED = ["matched", "reasoning", "confidence"]


def _tool_context_for_rule(rule: dict[str, Any],
                           tools_by_rule_key: dict[str, list[dict[str, Any]]],
                           tools_by_shape: dict[str, list[dict[str, Any]]],
                           tool_results: dict[str, dict[str, Any]],
                           ) -> tuple[list[dict[str, Any]], list[str]]:
    """Pick the tool result rows that apply to this rule and return
    (compact dicts for the prompt, binding_ids for audit)."""
    binding_ids: list[str] = []
    # Rule-scoped tools (tightest binding)
    for tb in tools_by_rule_key.get(rule["key"], []):
        binding_ids.append(tb["binding_id"])
    # Shape-scoped tools that aren't tied to any rule
    for tb in tools_by_shape.get(rule.get("shape_id", ""), []):
        if tb["binding_id"] in binding_ids:
            continue
        if not tb.get("rule_binding_id"):
            binding_ids.append(tb["binding_id"])

    compact: list[dict[str, Any]] = []
    for bid in binding_ids:
        rec = tool_results.get(bid)
        if not rec:
            continue
        compact.append({
            "tool": rec["tool_name"],
            "ok": rec["ok"],
            "result": rec["result"] if rec["ok"] else None,
            "error": rec["error"] if not rec["ok"] else "",
        })
    return compact, binding_ids


def evaluate_one_rule(cfg: EngineConfig, *, rule: dict[str, Any],
                      claim: dict[str, Any],
                      tool_context: list[dict[str, Any]],
                      stage: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one LLM evaluation. Returns (verdict, meta)."""
    # Resolve canonical SOP fields (Provider TIN, Received Date, …) from the
    # claim + tool results via sop_field_mapping.yaml. Additive: an empty
    # result simply omits the block and the prompt is unchanged.
    mapped_block = format_mapped_fields_block(
        resolve_sop_fields(claim, tool_context)
    )
    mapped_section = f"\n{mapped_block}\n" if mapped_block else ""

    # Surface the rule's own routing intent so the model can confirm/override it
    # by returning a `navigation` object. These come straight from the SOP and
    # are additive — when none apply the block is omitted and the prompt is
    # unchanged from before.
    routing_bits: list[str] = []
    if rule.get("step_number") is not None:
        routing_bits.append(f"this_step:    {rule.get('step_number')}")
    if rule.get("goto_step") is not None:
        routing_bits.append(
            f"sop_goto:     when this rule is Met the SOP routes to step {rule.get('goto_step')}")
    if rule.get("is_out_of_scope"):
        routing_bits.append(
            "out_of_scope: when Met this rule is OUT OF SCOPE — auditing should stop on this path")
    if rule.get("applicable_when"):
        routing_bits.append(
            f"applicable_when: {rule.get('applicable_when')} "
            "(if this claim does NOT satisfy applicable_when, set applicable=false)")
    routing_section = ("\nSOP ROUTING (use to set the optional `navigation`/`applicable` keys)\n"
                       "------------------------------------------------------------------\n"
                       + "\n".join(routing_bits) + "\n") if routing_bits else ""

    prompt = f"""You are a claims-audit policy evaluator. Decide whether the
following SOP rule applies to the given claim.

RULE
----
key:            {rule['key']}
source:         {rule['source']}
section:        {rule.get('section_label', '')}
decision_type:  {rule.get('decision_type', '')}
condition:      {rule.get('condition', '')}
action:         {rule.get('action', '')}
{mapped_section}{routing_section}
CLAIM
-----
{json.dumps(claim, default=str, indent=2)}

TOOL RESULTS (already fetched on your behalf; may be empty)
-----------------------------------------------------------
{json.dumps(tool_context, default=str, indent=2)}

Return a JSON object. These keys are REQUIRED:
  matched     boolean — true iff the rule's condition is satisfied by the claim
  reasoning   string  — concise explanation citing the claim fields / tool results you used
  confidence  number  — 0.0 to 1.0

You SHOULD also include these OPTIONAL keys to support an audit trail (omit
or leave empty only when you genuinely cannot determine them):
  status        string — one of "Met", "Not-Met", "Inconclusive". Use
                "Inconclusive" when required data is missing and could not be
                retrieved; "Met" when the condition holds; "Not-Met" otherwise.
  applicable    boolean — false ONLY when an `applicable_when` was given above
                and this claim does not satisfy it (the rule does not apply and
                should be skipped, NOT marked Not-Met). Defaults to true.
  navigation    object — where the audit should go next, mirroring the SOP's
                routing. Shape: {{"op": "goto"|"stop"|"next", "step_number": <int>}}.
                Use "goto" with the target step_number when the SOP says to skip
                ahead (e.g. "proceed to step 8 directly"); "stop" when the path
                is out of scope / auditing should halt; omit or "next" for the
                normal sequential flow.
  evidence_refs array of strings — dotted paths to the exact claim/tool fields
                you relied on, with their values, e.g.
                "facets_get_summary.body.Data.ClaimSummary.REC_CIV8.SBSB_ID=371468948".
  conditions    array of objects, one per atomic condition you evaluated, each:
                {{"condition": <text>, "evaluated": <bool>,
                 "using_fields": [<dotted field paths>],
                 "values": {{<field path>: <value>, "notes": <optional text>}}}}
"""
    verdict, meta = llm_call(
        cfg, prompt,
        agent_name=f"rule_eval[{rule['key']}]",
        stage=stage,
        fallback={"matched": False, "reasoning": "LLM fallback — all attempts failed",
                  "confidence": 0.0},
        provider="anthropic",
        expected_type=dict,
        required_keys=_EVAL_REQUIRED,
    )
    return verdict, meta
