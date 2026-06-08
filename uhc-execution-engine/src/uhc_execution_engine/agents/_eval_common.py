"""Shared rule-evaluation helpers for nodes 4 and 5.

Each rule produces one LLM call. The prompt gets:
* the parsed claim
* the rule's condition + action + decision_type
* any tool results scoped to that rule (via tools_by_rule_key) or to its
  shape (via tools_by_shape).
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..config import EngineConfig
from ..field_mapping import format_mapped_fields_block, resolve_sop_fields
from ..llm import llm_call

_EVAL_REQUIRED = ["matched", "reasoning", "confidence"]

# ── Tool-result compaction ───────────────────────────────────────────────────
# Tool results are embedded verbatim into the rule-eval prompt. A few tools
# (notably ``cbd_coverage``) return very large reference grids — e.g. ~734
# coverage rows ≈ 265k tokens — which on their own blow past the model's 200k
# context window (Anthropic 400 "prompt is too long"). When that happens the
# OpenAI fallback can't recover and the rule silently returns a "not matched"
# fallback, so coverage checks are skipped while the claim still reports CLEAN.
#
# To keep every rule actually evaluated we compact each tool result before it
# enters the prompt: (1) drop empty values and audit/id metadata — lossless for
# decisioning; (2) if a result is still oversized, keep the rows most relevant
# to the claim (token overlap) so the needle stays in.
_TOOL_RESULT_CHAR_BUDGET = 560_000   # per single tool result (~140k tokens)
_TOOL_CONTEXT_CHAR_BUDGET = 600_000  # all tool results combined (~150k tokens)

_EMPTY_SCALARS = {None, "", "N/A", "None", "null"}


def _is_empty(v: Any) -> bool:
    if isinstance(v, (dict, list)):
        return len(v) == 0
    return v in _EMPTY_SCALARS


def _is_noise_key(key: Any) -> bool:
    """Audit/identifier columns that carry no decisioning signal."""
    k = str(key).lower()
    if k in {"updatedby", "updateddate", "createdby", "createddate",
             "filename", "fileextension", "documentlinks", "per",
             "mappingid", "gridid", "cnpid"}:
        return True
    return k.endswith("id") or k.endswith("comment") or k == "comments"


def _prune(obj: Any) -> Any:
    """Recursively drop empty values + audit/id metadata. Lossless for rules."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if _is_noise_key(k):
                continue
            pv = _prune(v)
            if _is_empty(pv):
                continue
            out[k] = pv
        return out
    if isinstance(obj, list):
        return [_prune(v) for v in obj]
    return obj


def _tokens(obj: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", json.dumps(obj, default=str).lower()))


def _compact_one(result: Any, claim_toks: set[str], budget: int) -> Any:
    pruned = _prune(result)
    if len(json.dumps(pruned, default=str)) <= budget:
        return pruned
    # Oversized: find the dominant list and keep the rows most relevant to the
    # claim until the budget is spent.
    if isinstance(pruned, dict):
        list_key, best = None, -1
        for k, v in pruned.items():
            if isinstance(v, list):
                sz = len(json.dumps(v, default=str))
                if sz > best:
                    best, list_key = sz, k
        if list_key is not None:
            rows = pruned[list_key]
            ranked = sorted(rows, key=lambda r: -len(_tokens(r) & claim_toks))
            shell = {k: v for k, v in pruned.items() if k != list_key}
            acc = len(json.dumps(shell, default=str))
            kept: list[Any] = []
            for row in ranked:
                rj = len(json.dumps(row, default=str)) + 1
                if kept and acc + rj > budget:
                    break
                kept.append(row)
                acc += rj
            shell[list_key] = kept
            shell["_filtered"] = {
                "kept": len(kept), "total": len(rows),
                "note": ("rows filtered to those most relevant to this claim "
                         "to fit the model context window"),
            }
            return shell
    s = json.dumps(pruned, default=str)
    return {"_truncated_text": s[:budget], "_total_chars": len(s)}


def _compact_tool_context(tool_context: list[dict[str, Any]],
                          claim: dict[str, Any]) -> list[dict[str, Any]]:
    """Shrink tool results so the assembled prompt stays under the LLM limit."""
    if not tool_context:
        return tool_context
    claim_toks = _tokens(claim)
    remaining = _TOOL_CONTEXT_CHAR_BUDGET
    out: list[dict[str, Any]] = []
    for rec in tool_context:
        rec = dict(rec)
        if rec.get("result") is not None:
            budget = max(20_000, min(_TOOL_RESULT_CHAR_BUDGET, remaining))
            rec["result"] = _compact_one(rec["result"], claim_toks, budget)
        remaining -= len(json.dumps(rec, default=str))
        out.append(rec)
    return out


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
    # Compact oversized tool results (e.g. cbd_coverage's coverage grid) so the
    # assembled prompt stays within the model's context window. Without this a
    # single large tool result 400s the call and the rule is never evaluated.
    tool_context = _compact_tool_context(tool_context, claim)
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
