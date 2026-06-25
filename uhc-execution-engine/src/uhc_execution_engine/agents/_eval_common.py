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
# Tool results are embedded verbatim into the rule-eval prompt. Some tools
# (notably ``cbd_coverage``) return very large *reference grids* — e.g. 734
# coverage rows ≈ 326k tokens, and sometimes tens of thousands of rows — which
# on their own blow past the model's 200k context window (Anthropic 400 "prompt
# is too long"). When that happens the OpenAI fallback can't recover and the
# rule silently returns a "not matched" fallback, so coverage checks are skipped
# while the claim still reports CLEAN.
#
# Strategy (most → least precise):
#   1. Drop empty values + audit/id metadata (lossless for decisioning).
#   2. Coverage grids: keep ONLY the rows whose ``descCode`` matches one of the
#      claim's procedure codes (modifier-normalised). 734 rows → a handful.
#   3. If nothing matched (or the payload isn't a coverage grid) fall back to a
#      token-budgeted, claim-relevance-ranked trim so the needle still fits.
import functools

# Real token budgets (measured ratio for this JSON ≈ 3.25 chars/token, so a
# char budget over-counts badly — we size in tokens instead).
_TOOL_RESULT_TOKEN_BUDGET = 110_000   # per single tool result
_TOOL_CONTEXT_TOKEN_BUDGET = 130_000  # all tool results combined

_EMPTY_SCALARS = {None, "", "N/A", "None", "null"}

# Decision-relevant columns kept when projecting a coverage-grid row.
_COVERAGE_KEEP = {
    "descCode", "descName", "diagnosis", "serviceType", "covered",
    "authorization", "modifier", "telehealth", "behavioralProviderTypes",
    "placeOfService", "limitType", "limitAmount", "limitQuantity",
    "limitTimeframe", "ageLimits", "ageLimitsMin", "ageLimitsMax",
    "effectiveDate", "termDate", "lob", "parityClassification",
    "requiredCptCodes", "requiredCodes", "asamLevel", "stateMandate",
}
# A row "looks like" a coverage grid row when it carries this key.
_COVERAGE_ROW_KEY = "descCode"
# Claim keys that hold procedure / CPT / HCPCS codes.
_PROC_KEY_RE = re.compile(r"ipcd|proc.*c(?:o)?de|cpt|hcpcs|srv.*cd", re.IGNORECASE)


@functools.lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _ntok(s: str) -> int:
    enc = _encoder()
    if enc is not None:
        try:
            return len(enc.encode(s))
        except Exception:
            pass
    return len(s) // 3 + 1  # conservative when tiktoken is unavailable


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


def claim_procedure_codes(claim: dict[str, Any]) -> set[str]:
    """All procedure/CPT/HCPCS codes on the claim, plus modifier-stripped bases.

    e.g. a line with IPCD_ID="90837GT" yields {"90837GT", "90837"} so it matches
    a coverage grid keyed on the base CPT "90837".
    """
    codes: set[str] = set()

    def visit(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (str, int)) and _PROC_KEY_RE.search(str(k)):
                    raw = str(v).strip().upper()
                    if re.match(r"^[A-Z]?\d{3,5}", raw):
                        codes.add(raw)
                        m = re.match(r"^([A-Z]?\d{4,5})", raw)
                        if m:
                            codes.add(m.group(1))
                else:
                    visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)

    visit(claim)
    return codes


def _project_coverage_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items()
            if k in _COVERAGE_KEEP and not _is_empty(v)}


def _dominant_list(d: dict[str, Any]) -> tuple[str | None, list | None]:
    key, best = None, -1
    for k, v in d.items():
        if isinstance(v, list):
            sz = len(json.dumps(v, default=str))
            if sz > best:
                best, key = sz, k
    return key, (d[key] if key is not None else None)


def filter_coverage_grid(rows: list[Any], claim_codes: set[str]
                         ) -> list[dict[str, Any]] | None:
    """Return only the coverage rows for the claim's procedure code(s),
    projected to decision-relevant columns. ``None`` when no row matched."""
    if not claim_codes:
        return None
    kept = [
        _project_coverage_row(r) for r in rows
        if isinstance(r, dict) and str(r.get(_COVERAGE_ROW_KEY)) in claim_codes
    ]
    return kept or None


def _compact_one(result: Any, claim_codes: set[str], claim_toks: set[str],
                 budget_tokens: int) -> Any:
    pruned = _prune(result)
    if _ntok(json.dumps(pruned, default=str)) <= budget_tokens:
        return pruned

    if isinstance(pruned, dict):
        list_key, rows = _dominant_list(pruned)
        if list_key is not None and rows:
            shell = {k: v for k, v in pruned.items() if k != list_key}
            looks_coverage = isinstance(rows[0], dict) and _COVERAGE_ROW_KEY in rows[0]

            # 1) Coverage grid → keep only the claim's procedure-code rows.
            if looks_coverage:
                matched = filter_coverage_grid(rows, claim_codes)
                if matched is not None:
                    out = dict(shell)
                    out[list_key] = matched
                    out["_filtered"] = {
                        "kept": len(matched), "total": len(rows),
                        "claim_codes": sorted(claim_codes),
                        "note": ("coverage rows filtered to the claim's procedure "
                                 "code(s); audit columns dropped"),
                    }
                    if _ntok(json.dumps(out, default=str)) <= budget_tokens:
                        return out
                # No code match (or still too big) → project all rows and let
                # the relevance/budget trim below handle it.
                rows = [_project_coverage_row(r) if isinstance(r, dict) else r
                        for r in rows]

            # 2) Generic: rank rows by claim relevance, keep until budget spent.
            ranked = sorted(rows, key=lambda r: -len(_tokens(r) & claim_toks))
            acc = _ntok(json.dumps(shell, default=str))
            kept: list[Any] = []
            for row in ranked:
                t = _ntok(json.dumps(row, default=str))
                if kept and acc + t > budget_tokens:
                    break
                kept.append(row)
                acc += t
            shell[list_key] = kept
            shell["_filtered"] = {
                "kept": len(kept), "total": len(rows),
                "note": "rows trimmed to the most claim-relevant to fit context",
            }
            return shell

    s = json.dumps(pruned, default=str)
    return {"_truncated_text": s[: budget_tokens * 3], "_total_chars": len(s)}


def _compact_tool_context(tool_context: list[dict[str, Any]],
                          claim: dict[str, Any]) -> list[dict[str, Any]]:
    """Shrink tool results so the assembled prompt stays under the LLM limit."""
    if not tool_context:
        return tool_context
    claim_codes = claim_procedure_codes(claim)
    claim_toks = _tokens(claim)
    remaining = _TOOL_CONTEXT_TOKEN_BUDGET
    out: list[dict[str, Any]] = []
    for rec in tool_context:
        rec = dict(rec)
        if rec.get("result") is not None:
            budget = max(5_000, min(_TOOL_RESULT_TOKEN_BUDGET, remaining))
            rec["result"] = _compact_one(
                rec["result"], claim_codes, claim_toks, budget
            )
        remaining -= _ntok(json.dumps(rec, default=str))
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


# ── Domain guidance (provider selection) ─────────────────────────────────────
# The SOP IR flattens the gold provider-selection rules into per-choice rows and
# loses two things the rules depend on: (1) RULE-000's INN/OON *determination*
# procedure (a 2-point / 3-point provider match), and (2) the group-model gating
# that scopes each choice table. Without them the evaluator reads OON literally
# off CLCL_NTWK_IND / group_model and treats DOC360 box-27 (assignment of
# benefits) as "individual is billed", which mis-selects the 3rd/4th/5th (deny)
# choice on clean group-INN claims. This block re-injects that context at
# execution time, scoped to provider-selection rules so other prompts are
# unchanged. It is policy guidance only — it never dictates a verdict.
_PROVSEL_SIGNALS = (
    "provider entity type", "individual is billed", "network indicator",
    "group record", "group model", "provider selection",
    "inn", "oon", "in network", "out of network",
    "2 point match", "3 point match", "1st choice", "2nd choice", "3rd choice",
)

_PROVSEL_GUIDANCE = """\
DOMAIN GUIDANCE — PROVIDER SELECTION (apply before deciding `matched`)
---------------------------------------------------------------------
1. INN vs OON is DERIVED, never read literally. Do NOT conclude OON/INN from
   `CLCL_NTWK_IND` or the facet-extension `group_model` (e.g. "AN" = *Assumed*
   Non-network is an assumption, not a determination). Instead match the billed
   provider against the FACETS provider-details records:
     • multiple records match on 2 points (Tax ID/EIN + NPI)  -> INN
     • exactly one record matches on all 3 points (Tax ID + NPI + name/address) -> OON
   Then reconcile with the network indicator. If the provider-details records
   needed for this match are not present in the tool results, you CANNOT
   determine OON — set status="Inconclusive" (do NOT default to OON/deny).

2. "Individual is billed" means an INDIVIDUAL/rendering provider appears on the
   DOC360 claim image: box 24 (Rendering NPI) and/or box 33 (Servicing
   Physician/Supplier Name) populated with a person. DOC360 box 27
   ("A/ASSIGNED", "Y/YES ASSIGNED") is ACCEPT-ASSIGNMENT (assignment of
   benefits) — it is NOT evidence that an individual is billed. Never use box 27
   to satisfy an "individual is billed" condition.

3. Group-model gating: the choice tables are scoped by the FACETS group model —
   1A / AN / "No Group Model" -> step 4; 2A / 2I -> step 5; 3A -> step 6;
   3B -> step 7. Only the table matching THIS claim's group_model applies. If
   this rule's choice belongs to a different group-model table than the claim's
   actual group_model, set applicable=false (do NOT mark it Met/Not-Met).

4. A group-billed claim whose provider resolves to INN (group properly located
   and matched) selects the 1st/2nd choice and is CLEAN — it is NOT a provider-
   selection defect. Only select a deny choice when the derived determination
   (per 1-3 above), not a face-value indicator, genuinely supports it.
"""


def _domain_context(rule: dict[str, Any]) -> str:
    """Return scoped domain guidance for provider-selection rules, else "".

    Detection is text-based (the flattened rule rows don't carry their SOP name),
    keyed off the distinctive provider-selection vocabulary in the rule's
    condition/action/section. Additive: non-matching rules get an empty block and
    their prompt is unchanged.
    """
    blob = " ".join(str(rule.get(k, "")) for k in
                    ("condition", "action", "section_label")).lower()
    hits = sum(1 for s in _PROVSEL_SIGNALS if s in blob)
    return f"\n{_PROVSEL_GUIDANCE}" if hits >= 2 else ""


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

    # Re-inject the determination procedure + field semantics the IR flattening
    # dropped, scoped to provider-selection rules. Additive elsewhere.
    domain_section = _domain_context(rule)

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
{mapped_section}{routing_section}{domain_section}
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
