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
        # LOB-gated tools were never invoked — keep their (absent) output out of
        # the prompt entirely, but keep the binding id in ``binding_ids`` so the
        # trace can still surface them under ``tools_skipped``.
        if rec.get("skipped"):
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

5. GROUP-MODEL SET MEMBERSHIP IS "OR". When a choice table is scoped to several
   group models (e.g. "2A, 2I" or "2A/2I"), the claim's SINGLE group model
   matching ANY one listed value satisfies membership. Do NOT require the claim
   to match every listed model (it only ever has one).

6. CONFIRMING A CORRECT OON DENIAL (do not false-negative it). When a group
   claim is genuinely OON (derived per 1), its group model is 3B, the provider
   is a group (`PRPR_ENTITY='G'`), the line was denied with an OON
   provider-selection EOB (`CDML_DISALL_EXCD` in {FOF, FOD, FOE}) and nothing was
   paid (`total_paid`=0), the deny choice IS correctly selected — set
   matched=true and CONFIRM the existing denial (network_basis="provider_match").
   This is a CLEAN confirmation of a correct system action, not a new defect and
   not a false positive. Mirror the analogous FOE 2A/2I and FOD group-only cases.
"""


# ── Domain guidance (subscriber-ID verification) ─────────────────────────────
# Physician Claim Checklist sub-rule "Subscriber ID" compares the FACETS
# Subscriber ID (SBSB_ID) against the Doc360 image "Insured's ID Number". The IR
# gives the evaluator only SBSB_ID, so a naive string compare reports a false
# mismatch (and a prior heuristic then mislabeled it "masked / Not Applicable").
# In reality the Doc360 image carries the full MEMBER ID = subscriber base +
# member suffix, sometimes with a plan/product prefix; and FACETS also holds
# alternate identifiers on a Transfer-Subscriber-Family "Additional ID" screen
# our tools do not return. This block re-injects those semantics, scoped to the
# subscriber-ID check. Policy guidance only — it never dictates a verdict.
_SUBSCRIBER_SIGNALS = (
    "subscriber id", "insured's id", "insured id", "member id", "sbsb_id",
    "sbsb id",
)

_SUBSCRIBER_GUIDANCE = """\
DOMAIN GUIDANCE — SUBSCRIBER ID (image vs FACETS; apply before deciding `matched`)
----------------------------------------------------------------------------------
The Doc360 claim image "Insured's ID Number" is the full MEMBER ID. The FACETS
`SBSB_ID` is the SUBSCRIBER BASE. They identify the SAME member when they differ
only by a member/dependent suffix and/or a plan/product prefix, e.g.:
  SBSB_ID K61297021  == image K6129702101 (base + suffix 01)
  SBSB_ID 999095985  == image M2K999095985 (plan prefix + base)
  SBSB_ID C77772744  == image OSC7777274401 (prefix + base + suffix)
  SBSB_ID 00112854   == image STAS00112854 (prefix + base)

DECIDE `matched` like this:
0. CHECK EVERY STORED FACETS ID LOCATION, not just SBSB_ID. Compare the image id
   against ALL subscriber-id fields present in the tool results — `SBSB_ID`,
   `CLMF_INPUT_SBSB_ID`, `MEME_HEALTH_ID` (the Facets Standard Unique Health ID),
   and any duplicate `SubscriberID` field. A match against ANY of them is a Met.
1. Normalize both values — uppercase and strip all non-alphanumerics (and ignore
   leading zeros for purely numeric ids).
2. If the normalized SBSB_ID (or MEME_HEALTH_ID) equals, or is a contiguous
   substring of, the normalized image id (or their digit cores contain one
   another), they are the SAME member -> Subscriber ID is MATCHED (status="Met").
   A trailing member suffix and/or a leading plan/product prefix is NOT a
   discrepancy.
3. If they do NOT relate by base+suffix/prefix to ANY stored location, the image
   id may still be a valid
   alternate identifier recorded in FACETS under `Transfer Subscriber Family >
   Subscriber > Additional ID` — a field NOT present in these tool results. In
   that case you CANNOT confirm or refute the match from the available data: set
   status="Inconclusive" (the Additional ID must be checked manually). Do NOT
   report a discrepancy/defect, and do NOT claim a clean match, based solely on
   SBSB_ID != image id.
4. Never treat a Subscriber-ID mismatch as a claim defect on its own — this is a
   verification/reconciliation check, not an adverse determination.
"""


# ── Domain guidance (timely filing) ──────────────────────────────────────────
# The Timely-Filing SOP (OBH Facets) flattens into ~18 steps whose deterministic
# intent is lost in the IR: the Step-1 TF0/TF1 gate is a *scope* gate (not a
# hard stop), "history" must exclude self-adjustments / self-matches / later
# claims, a within-limit calculation is CLEAN (never a deny), and group/plan
# names must match on word boundaries (MEDICA != MEDICARE/MEDICAID, GE !=
# ADVANTAGE). Without this the evaluator (a) stops the whole SOP at Step 1 on
# non-TF0/TF1 claims, (b) counts a self-adjustment as prior filing history, (c)
# denies "no history" without comparing days vs the limit, and (d) false-matches
# group keywords by substring. This block re-injects that intent, scoped to
# timely-filing rules. Policy guidance only — it never dictates a verdict.
_TF_SIGNALS = (
    "timely filing", "tf0", "tf1", "filing limit", "filing deadline",
    "received date", "received within", "days to file", "submission date",
    "within the", "beyond the", "365-day", "365 day", "claim history",
)

_TF_GUIDANCE = """\
DOMAIN GUIDANCE — TIMELY FILING (apply before deciding `matched`)
----------------------------------------------------------------
1. STEP-1 GATE IS SCOPE, NOT A STOP. "Is your claim/line denying for TF1 or
   TF0?" only decides whether the TF-denial-review branch APPLIES. If the claim
   is NOT denying for a TF0/TF1 EOB code, set applicable=false for the Step-1
   gate rows (they are Not Applicable) and CONTINUE evaluating the remaining
   in-scope timely-filing steps. Do NOT halt the whole SOP at Step 1, and do NOT
   append TF0/TF1 disclaimer boilerplate to `reasoning`.

2. WHAT COUNTS AS FILING "HISTORY". A prior claim is valid filing history only
   if it is a genuinely DISTINCT, EARLIER claim for the same member/DOS/provider.
   EXCLUDE from history: (a) the audited claim matching ITSELF (same base
   CLCL_ID / claim number); (b) self-adjustment / replacement / void rows of the
   same claim; (c) candidates whose claim number or received/created date is
   LATER than the audited claim. If the only "match" is a self-adjustment or a
   self-match, treat the claim as having NO history.

3. NO-HISTORY IS NOT AUTOMATICALLY LATE. When there is no valid prior history,
   do NOT conclude untimely or deny on "no history" alone. Compute the elapsed
   days from the service/received date and compare against the plan's filing
   limit; conclude untimely ONLY when days > limit.

4. WITHIN-LIMIT IS CLEAN. If the received date is within the filing limit, the
   claim WAS filed timely — this is a clean, non-adverse finding. A row that
   establishes "received N days ... WITHIN the L-day limit" must NOT carry a
   DENY/STOP/REFER/PEND disposition. If such a claim was nonetheless denied for
   TF0/TF1, the timely-filing calculation is within-limit and the denial is the
   defect to report — not the calculation.

5. GROUP / PLAN NAME MATCHING (step 4 group tables). Match plan/group keywords on
   WHOLE-WORD boundaries, never as substrings: "MEDICA" must NOT match inside
   "MEDICARE" or "MEDICAID"; "GE" must NOT match inside "ADVANTAGE". Consider the
   group name `GRGR_NAME` in addition to `PLDS_DESC` when identifying the group
   (e.g. Motion Picture Industry / MPI is carried on GRGR_NAME).
"""


# ── Domain guidance (coverage / covered-benefit) ─────────────────────────────
# The Access-Covered-Benefit SOP checks the claim's procedure codes against the
# `cbd_coverage` grid. The grid is keyed by BENEFIT CATEGORY (`descCode` /
# `descName`, e.g. "Office/Outpatient Visit", "Psychotherapy"), NOT by raw CPT.
# A naive lookup that expects the CPT to appear literally as a `descCode` finds
# nothing and reports the code "not found" even when the sub-rule statement
# already establishes it is Covered. This block tells the evaluator to map the
# CPT to its benefit category first. Policy guidance only — never a verdict.
_COVERAGE_SIGNALS = (
    "covered benefit", "cbd", "coverage", "desccode", "benefit category",
    "is the code covered", "procedure code covered", "cbd_coverage",
    "covered benefit document", "access covered benefit",
)

_COVERAGE_GUIDANCE = """\
DOMAIN GUIDANCE — COVERAGE / COVERED BENEFIT (apply before deciding `matched`)
-----------------------------------------------------------------------------
1. THE COVERAGE GRID IS KEYED BY BENEFIT CATEGORY, NOT CPT. `cbd_coverage` rows
   carry a `descCode`/`descName` that names a benefit CATEGORY (e.g.
   "Office/Outpatient Visit", "Psychotherapy", "Behavioral Health"), not the raw
   CPT/HCPCS on the claim line. Map the claim's procedure code to its benefit
   category before reading coverage, e.g. E&M office visits 99202-99215 ->
   Office/Outpatient Visit; psychotherapy 90832-90838 (incl. add-on 90833) ->
   Psychotherapy / Behavioral Health.
2. "NOT FOUND" vs "NOT COVERED" vs "COVERED". Only say a code is NOT FOUND when
   NEITHER the CPT NOR its benefit category appears anywhere in the plan grid.
   Say NOT COVERED only when the mapped benefit-category row's `covered` = No.
   Otherwise the code is COVERED. A CPT that simply isn't a literal `descCode` is
   NOT "not found".
3. DO NOT CONTRADICT AN ESTABLISHED DETERMINATION. If the rule `statement` /
   `action` already says a code is Covered (or a prior sub-rule established it),
   do NOT then describe that same code as "not found" in `reasoning`.
"""


# ── Domain guidance (duplicate claim) ────────────────────────────────────────
# The Duplicate SOP flattens into a Step 1-8 walk whose deterministic intent is
# lost: a Box-12A frequency 7/8 (corrected/replacement/void) claim is OUT OF
# SCOPE for duplicate auditing (route to manual review, not a duplicate defect);
# "history" must exclude self-adjustments / self-matches (same as timely
# filing); and when a claim is ALREADY denying as a duplicate via the Facets
# ultra-blue CDD edit (an EOB on facets_get_line_details `CDML_DISALL_EXCD`), the
# Step 7/8 "confirm the CDD duplicate" row is a SYSTEM confirmation of an
# existing system action, NOT a new adverse determination — it is CLEAN. This
# block re-injects that intent, scoped to duplicate rules. Policy guidance only.
_DUP_SIGNALS = (
    "duplicate", "cdd", "corrected claim", "void", "replacement claim",
    "frequency 7", "frequency 8", "bill type", "box 12a", "resubmission",
    "cdml_disall_excd", "ultra-blue", "ultra blue",
)

_DUP_GUIDANCE = """\
DOMAIN GUIDANCE — DUPLICATE CLAIM (apply before deciding `matched`)
------------------------------------------------------------------
1. CORRECTED / VOID (freq 7 or 8) IS OUT OF SCOPE. If the claim's Box-12A
   submission frequency / bill-type is 7 (replacement/corrected) or 8 (void),
   the duplicate check cannot be confirmed or ruled out from claim data alone —
   set status="Inconclusive" (manual review) and route the duplicate branch out
   of scope. Do NOT deny it as a duplicate and do NOT clear it CLEAN by default.

2. WHAT COUNTS AS DUPLICATE "HISTORY". A history/candidate claim is a real
   duplicate only if it is a DISTINCT claim (different base CLCL_ID / claim
   number). EXCLUDE the audited claim matching ITSELF and any self-adjustment /
   replacement / void row of the same claim. If the only "match" the duplicate
   tool returns is a self-match or self-adjustment, there is NO duplicate.

3. CDD SYSTEM DENIAL IS NOT A NEW DEFECT. When the claim is already denying as a
   duplicate through the Facets ultra-blue CDD edit — i.e. `facets_get_line_details`
   shows the duplicate EOB on the line (`CDML_DISALL_EXCD`) — the Step 7/8 rule
   is CONFIRMING an existing SYSTEM action, not applying a new denial. Report it
   with decision_type="SYSTEM" (non-adverse); the audit outcome is CLEAN (the
   system correctly denied the duplicate). Only a duplicate the auditor must act
   on (unconfirmed / needs manual dedup) is adverse.
"""


_MATCHING_CONTRACT = """\
MATCHING CONTRACT (read this BEFORE deciding `matched`)
-------------------------------------------------------
1. `matched` is decided SOLELY by whether the rule's `condition` is satisfied by
   the claim / tool facts. The `action` only describes the CONSEQUENCE when the
   condition holds — it is NEVER a matching criterion. Do not set matched=true
   just because the `action` text mentions an error / denial / "should be denied".
2. Every atomic clause in the `condition` must hold. If your own reasoning
   establishes a fact that violates ANY clause of the condition (e.g. the
   condition requires "individual is NOT billed" but you find the individual IS
   billed), then the rule is NOT matched — set matched=false / status="Not-Met".
3. If the `condition` and the `action` contradict each other (e.g. condition says
   "NOT billed" while the action says "if billed → deny"), the `condition` is
   authoritative for `matched`. Evaluate the condition as written, call out the
   contradiction in `reasoning`, and lower `confidence`.
"""


# Output-format contract. This is fully static — it contains no per-call data —
# so it lives in the (cacheable) system prompt rather than being re-templated
# into every rule's prompt. Wording that used to say "above" (referring to the
# old single-string layout) is phrased order-neutrally so it stays accurate now
# that the RULE / ROUTING sections follow the claim in the user turn.
_OUTPUT_SCHEMA = """\
Return a JSON object. These keys are REQUIRED:
  matched     boolean — true iff the rule's CONDITION is satisfied by the claim
                (per the MATCHING CONTRACT; the `action` never makes it true)
  reasoning   string  — concise explanation citing the claim fields / tool results you used
  confidence  number  — 0.0 to 1.0

You SHOULD also include these OPTIONAL keys to support an audit trail (omit
or leave empty only when you genuinely cannot determine them):
  status        string — one of "Met", "Not-Met", "Inconclusive". Use
                "Inconclusive" when required data is missing and could not be
                retrieved; "Met" when the condition holds; "Not-Met" otherwise.
  applicable    boolean — false ONLY when an `applicable_when` was provided for
                this rule and this claim does not satisfy it (the rule does not
                apply and should be skipped, NOT marked Not-Met). Defaults to true.
  navigation    object — where the audit should go next, mirroring the SOP's
                routing. Shape: {"op": "goto"|"stop"|"next", "step_number": <int>}.
                Use "goto" with the target step_number when the SOP says to skip
                ahead (e.g. "proceed to step 8 directly"); "stop" when the path
                is out of scope / auditing should halt; omit or "next" for the
                normal sequential flow.
  network_basis string — PROVIDER-SELECTION rules only: how you determined
                INN vs OON — "provider_match" (you matched the billed provider
                against FACETS provider-details records on Tax ID/NPI/name),
                "literal_indicator" (you used only CLCL_NTWK_IND / group_model
                face value), "inconclusive" (the provider-details records needed
                were missing), or "not_applicable". Be honest: if a deny hinges
                on OON and you did NOT confirm it via a provider-record match,
                use "literal_indicator" or "inconclusive" — the engine will then
                route the claim to manual review instead of auto-denying.
  evidence_refs array of strings — dotted paths to the exact claim/tool fields
                you relied on, with their values, e.g.
                "facets_get_summary.body.Data.ClaimSummary.REC_CIV8.SBSB_ID=371468948".
  conditions    array of objects, one per atomic condition you evaluated, each:
                {"condition": <text>, "evaluated": <bool>,
                 "using_fields": [<dotted field paths>],
                 "values": {<field path>: <value>, "notes": <optional text>}}
"""


# Fully static system prompt: role framing + matching contract + output schema.
# Identical for every rule of every claim, so on the Anthropic path it is sent
# once as a cache_control block and billed at the cache rate thereafter.
_SYSTEM_PROMPT = (
    "You are a claims-audit policy evaluator. Decide whether the SOP rule "
    "provided in the next message applies to the given claim.\n\n"
    f"{_MATCHING_CONTRACT}\n{_OUTPUT_SCHEMA}"
)


def _domain_context(rule: dict[str, Any]) -> str:
    """Return scoped domain guidance for provider-selection rules, else "".

    Detection is text-based (the flattened rule rows don't carry their SOP name),
    keyed off the distinctive provider-selection vocabulary in the rule's
    condition/action/section. Additive: non-matching rules get an empty block and
    their prompt is unchanged.
    """
    blob = " ".join(str(rule.get(k, "")) for k in
                    ("condition", "action", "section_label")).lower()
    parts: list[str] = []
    if sum(1 for s in _PROVSEL_SIGNALS if s in blob) >= 2:
        parts.append(_PROVSEL_GUIDANCE)
    # Subscriber-ID verification: the rule compares an "Insured's ID"/"Subscriber
    # ID" against FACETS. Trigger on any subscriber-id signal (the check is a
    # single, distinctively-worded rule so one hit is enough).
    if any(s in blob for s in _SUBSCRIBER_SIGNALS):
        parts.append(_SUBSCRIBER_GUIDANCE)
    # Timely filing: the SOP is distinctively worded (TF0/TF1, filing limit,
    # received/within/beyond). Require >=2 signals so an incidental "received
    # date" mention on an unrelated rule doesn't pull in the whole block.
    if sum(1 for s in _TF_SIGNALS if s in blob) >= 2:
        parts.append(_TF_GUIDANCE)
    # Duplicate claim: distinctive vocabulary (duplicate / CDD / corrected /
    # freq 7|8 / CDML_DISALL_EXCD). Require >=2 signals so a stray "duplicate"
    # mention elsewhere doesn't inject the whole block.
    if sum(1 for s in _DUP_SIGNALS if s in blob) >= 2:
        parts.append(_DUP_GUIDANCE)
    # Coverage / covered-benefit: the check maps claim CPTs against the CBD grid.
    if sum(1 for s in _COVERAGE_SIGNALS if s in blob) >= 2:
        parts.append(_COVERAGE_GUIDANCE)
    return ("\n" + "\n".join(parts)) if parts else ""


def _workbench_context_section(rule: dict[str, Any]) -> str:
    """Auditor-authored per-SOP context, injected verbatim for this SOP's rules.

    Sourced from ``Workbench.config['extra_context']`` (set in the builder UI)
    and attached to every rule of that SOP by ``rule_loader``. Layered on top of
    any hardcoded ``_domain_context`` guidance — it does NOT replace it. Purely
    additive: rules whose SOP has no context get an empty block and an unchanged
    prompt. This is interpretive guidance only; it never dictates the verdict.
    """
    ctx = str(rule.get("sop_extra_context") or "").strip()
    if not ctx:
        return ""
    return (
        "\nSOP CONTEXT (auditor-provided guidance for THIS SOP; use it to "
        "interpret the rule and the claim correctly)\n"
        "------------------------------------------------------------------"
        "----------------------------------------\n"
        f"{ctx}\n"
    )


def _rule_context_section(rule: dict[str, Any]) -> str:
    """Auditor-authored per-RULE context, injected verbatim for THIS rule only.

    Sourced from ``sop_rules[i].additional_context`` (the "Add context" dialog in
    the builder UI) and attached by ``rule_loader``. Complements the per-SOP
    ``sop_extra_context``: this is scoped to a single rule/sub-rule so an auditor
    can steer one step (e.g. "this step is not applicable unless the claim is a
    TF1/TF0 denial") without touching the rest of the SOP. Purely additive
    interpretive guidance — it never dictates the verdict. Empty for rules with
    no context, so the prompt is unchanged.
    """
    ctx = str(rule.get("additional_context") or "").strip()
    if not ctx:
        return ""
    # Avoid double-injection: when the per-rule context was materialized from the
    # SOP-level context (same text), it is already rendered by
    # ``_workbench_context_section`` — skip the duplicate here.
    sop_ctx = str(rule.get("sop_extra_context") or "").strip()
    if sop_ctx and (ctx == sop_ctx or ctx in sop_ctx):
        return ""
    return (
        "\nRULE CONTEXT (auditor-provided guidance for THIS specific rule; use it "
        "to decide applicability and matching correctly)\n"
        "------------------------------------------------------------------"
        "----------------------------------------\n"
        f"{ctx}\n"
    )


def _prior_findings_section(prior_findings: list[dict[str, Any]] | None) -> str:
    """Render the already-evaluated rules of THIS SOP (top-to-bottom) so the
    current rule can honor precedence (e.g. a prioritized "1st/2nd/3rd choice"
    ladder). Empty when nothing has been evaluated yet — additive."""
    if not prior_findings:
        return ""
    lines: list[str] = []
    for f in prior_findings:
        if f.get("skipped"):
            verdict_txt = "SKIPPED"
        elif f.get("matched"):
            verdict_txt = "MATCHED (condition satisfied)"
        else:
            verdict_txt = "not matched"
        label = str(f.get("label") or "").strip()
        dt = str(f.get("decision_type") or "").strip()
        tag = f" [{label}]" if label else ""
        tag += f" {{{dt}}}" if dt else ""
        lines.append(f"  - {f.get('key')}{tag} -> {verdict_txt}")
        reason = str(f.get("reasoning") or "").strip()
        if reason:
            lines.append(f"      finding: {reason}")
    return (
        "\nPRIOR RULE FINDINGS — already evaluated IN THIS SOP, in order (top-to-bottom)\n"
        "---------------------------------------------------------------------------\n"
        + "\n".join(lines)
        + "\n(Honor precedence: if THIS rule is a lower-priority choice whose condition "
        "requires that earlier choices were NOT satisfied, and one of those earlier "
        "choices above is MATCHED, then this rule's condition is NOT satisfied — set "
        "matched=false. Do NOT contradict a factual determination an earlier sibling "
        "already made (e.g. if a sibling established the individual IS billed, do not "
        "now claim the individual is NOT billed).)\n"
    )


def evaluate_one_rule(cfg: EngineConfig, *, rule: dict[str, Any],
                      claim: dict[str, Any],
                      tool_context: list[dict[str, Any]],
                      stage: str,
                      prior_findings: list[dict[str, Any]] | None = None,
                      ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one LLM evaluation. Returns (verdict, meta).

    ``prior_findings`` are the verdicts of rules already evaluated in this SOP
    (the engine runs a SOP's rules sequentially), surfaced so this rule can
    respect a prioritized choice ladder.
    """
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

    # Identified Line of Business for this claim. Surfaced so an LOB-specific
    # rule (e.g. "Medicaid eligibility") can mark itself applicable=false when
    # the claim's LOB does not match — without dictating the verdict.
    lob_label = str(claim.get("line_of_business") or "").strip()
    lob_section = (
        f"\nCLAIM LINE OF BUSINESS: {lob_label}\n"
        "(If this rule applies only to a different Line of Business than the "
        "claim's, set applicable=false — it is out of scope, NOT Not-Met.)\n"
    ) if lob_label else ""

    # Re-inject the determination procedure + field semantics the IR flattening
    # dropped, scoped to provider-selection rules. Additive elsewhere.
    domain_section = _domain_context(rule)
    # Auditor-authored per-SOP context from the builder UI (additive, layered on
    # top of any hardcoded domain guidance above).
    sop_context_section = _workbench_context_section(rule)
    # Auditor-authored per-RULE context ("Add context" in the builder UI). Scoped
    # to this one rule; additive on top of the SOP-level context above.
    rule_context_section = _rule_context_section(rule)
    # Verdicts of earlier rules in this SOP (sequential evaluation) so this rule
    # can honor a prioritized choice ladder.
    prior_section = _prior_findings_section(prior_findings)

    # Prompt is split into three parts so the invariant portions can be cached
    # by Anthropic (billed at ~10% on repeat calls within one claim's run):
    #   • system_prompt  — role + matching contract + output schema (fully static)
    #   • claim_block     — the parsed claim (identical across this claim's rules)
    #   • tail            — the per-rule content that actually changes each call
    # The content is byte-for-byte the same as before, only reordered so the
    # cache prefix is stable; the verdict/rationale is unaffected.
    claim_block = (
        "CLAIM\n"
        "-----\n"
        f"{json.dumps(claim, default=str, indent=2)}"
    )

    tail = f"""RULE
----
key:            {rule['key']}
source:         {rule['source']}
section:        {rule.get('section_label', '')}
decision_type:  {rule.get('decision_type', '')}
condition:      {rule.get('condition', '')}
action:         {rule.get('action', '')}
{mapped_section}{lob_section}{routing_section}{domain_section}{sop_context_section}{rule_context_section}{prior_section}
TOOL RESULTS (already fetched on your behalf; may be empty)
-----------------------------------------------------------
{json.dumps(tool_context, default=str, indent=2)}"""

    verdict, meta = llm_call(
        cfg, tail,
        agent_name=f"rule_eval[{rule['key']}]",
        stage=stage,
        fallback={"matched": False, "reasoning": "LLM fallback — all attempts failed",
                  "confidence": 0.0},
        provider="anthropic",
        expected_type=dict,
        required_keys=_EVAL_REQUIRED,
        system_prompt=_SYSTEM_PROMPT,
        cache_prefix=claim_block,
    )
    return verdict, meta
