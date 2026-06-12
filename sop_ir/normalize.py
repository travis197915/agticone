"""Pure text + heuristic helpers shared by the IR layer.

These functions were the single source of the YAML importer's planning logic
(``sop_ingestion/management/commands/import_sop_yaml.py``). They are lifted
here verbatim (behaviour-preserving) so that BOTH ingestion doors — the YAML
importer and the HTML/PDF pipeline — classify dispositions, extract codes and
infer routing through *one* implementation, killing the divergent duplicate
that lived in ``a11_write_postgres.py``.

No Django / pydantic imports: this module is safe to import from the
standalone ``uhc-sop-ingestion`` pipeline package.
"""
from __future__ import annotations

import re
from typing import Any

# ── out-of-scope detection ────────────────────────────────────────────────────
_OOS_MARKERS = (
    "out of scope",
    "out-of-scope",
    "stop further auditing",
    "stop auditing further",
)

# ── decision classification ───────────────────────────────────────────────────
# Mirrors AuditDecision.DECISION_CHOICES (see sop_ingestion/models.py). Kept as
# a plain set so this module stays Django-free; sop_ir.schema.DecisionType is the
# typed mirror and sop_ir/tests asserts the two never drift.
_VALID_DECISIONS = {
    "DENY", "ALLOW", "BYPASS", "PEND", "REFER", "SYSTEM", "STOP", "WAIVE",
    "CONDITIONAL",
}

# Phrases that mark a genuine "send this claim to a human" referral, as opposed
# to a "refer to <document/SOP/list>" citation.
_REVIEWER_TARGETS = (
    "NURSE", "SPECIALIST", "MEDICAL DIRECTOR", "CLINICAL REVIEW", "MANUAL REVIEW",
    "REVIEWER", "ADJUSTER", "REFER THE CLAIM", "REFER FOR", "REFER TO A ",
)


def classify_decision(text: str) -> str:
    """Map a rule's action/output text to an adjudication disposition.

    CRITICAL: only *terminal dispositions* (DENY/REFER/PEND/STOP/...) may drive
    the claim verdict. Flow-control language — "proceed to next step", "skip to
    step N", "go to step N", "retrieve ...", "call <tool>" — and "out of scope"
    (a clean line-item exclusion, tracked separately via is_out_of_scope) are
    NOT dispositions. Classifying routing as REFER/STOP was producing false
    DEFECT/REFER verdicts on clean claims, so routing now falls through to the
    neutral CONDITIONAL bucket and never outranks ALLOW in the aggregator.
    """
    # Normalize hyphenated/underscored spellings so "out-of-scope" and
    # "out of scope" (and "skip-to" / "skip to") are treated identically.
    t = re.sub(r"[-_]+", " ", (text or "").upper())
    if "CDD" in t:                                    return "DENY"
    if "DENY" in t or "DENIAL" in t or "DENIED" in t: return "DENY"
    if "BYPASS" in t or "OVERRIDE" in t:              return "BYPASS"
    if "PEND" in t:                                   return "PEND"
    if "WAIVE" in t:                                  return "WAIVE"
    # Word-boundary match so "disallowance" / "CDML_DISALL_EXCD" (a data field
    # name, not a disposition) does NOT false-match the ALLOW disposition.
    if re.search(r"\bALLOW\b", t) or ("PROCESS" in t and "F3" in t): return "ALLOW"
    # A genuine referral disposition routes the claim to a human reviewer. In
    # this SOP corpus "refer to <SOP / list / table / section>" is a *citation*,
    # not a disposition — so only treat explicit reviewer routing as REFER.
    if "REFERRAL" in t or ("REFER" in t and any(
        kw in t for kw in _REVIEWER_TARGETS)):        return "REFER"
    # A genuine hard stop — but NOT "out of scope" (a clean exclusion / handoff,
    # tracked via is_out_of_scope) and NOT the routing verbs below.
    if "STOP" in t and "OUT OF SCOPE" not in t:       return "STOP"
    # "proceed", "skip to", "go to step", "retrieve", "call <tool>",
    # "refer to <document>" and "out of scope" are routing / data-gathering,
    # not dispositions.
    return "CONDITIONAL"


def extract_codes(text: str) -> dict[str, list[str]]:
    """Pull EOB / EX / denial / system-action codes out of free text.

    Handles the code families that appear in the OBH duplicate-handling SOP:
    EOB/reason codes (E51, F51, F24, F55, W46, W47), EX codes (003, 020, 001),
    denial edits (CDD) and system actions (F3, F4, F5).
    """
    T = (text or "").upper()
    eob, ex, denial, sysact = [], [], [], []

    # EX codes: "EX code 003", "EX code 020/001", "EX code 020 or EX code 001"
    for chunk in re.findall(r"EX\s*(?:CODE\s*)?((?:\d{3})(?:\s*/\s*\d{3})*)", T):
        for n in re.findall(r"\d{3}", chunk):
            ex.append(n)

    # EOB / reason codes: a letter E/F/W followed by exactly two digits
    for c in re.findall(r"\b([EFW]\d{2})\b", T):
        eob.append(c)

    # System actions: standalone F3 / F4 / F5 (e.g. "(F3)", "pend (F5)")
    for c in re.findall(r"\bF([3-5])\b", T):
        sysact.append("F" + c)

    if "CDD" in T:
        denial.append("CDD")

    dedupe = lambda xs: list(dict.fromkeys(xs))
    return {
        "eob": dedupe(eob),
        "ex": dedupe(ex),
        "denial": dedupe(denial),
        "sysact": dedupe(sysact),
    }


def extract_goto(text: str) -> int | None:
    """Parse 'skip to Step 4', 'proceed to step 9', 'step 8 directly' -> int."""
    t = (text or "").lower()
    m = re.search(r"(?:skip to|proceed to|go to|directly to|jump to)\s+step\s+(\d+)", t)
    if m:
        return int(m.group(1))
    m = re.search(r"step\s+(\d+)\s+directly", t)
    if m:
        return int(m.group(1))
    return None


def as_text(v: Any, joiner: str = "\n") -> str:
    """Normalize a YAML scalar / list into a single clean string."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        parts = [as_text(x, joiner) for x in v]
        return joiner.join(p for p in parts if p and p.strip() and p.strip() != "-")
    s = str(v).strip()
    return "" if s == "-" else s


def as_list(v: Any) -> list[str]:
    """Normalize a YAML scalar / list into a list of clean strings (drops '-')."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            s = as_text(x)
            if s and s.strip() and s.strip() != "-":
                out.append(s)
        return out
    s = str(v).strip()
    return [] if (not s or s == "-") else [s]


def is_out_of_scope(*chunks: Any) -> bool:
    blob = " ".join(as_text(c, " ") for c in chunks).lower()
    return any(m in blob for m in _OOS_MARKERS)


def infer_aggregation(output: str, has_children: bool) -> str:
    if not has_children:
        return "LEAF"
    o = (output or "").lower()
    if "any pair" in o or "any of the above" in o:
        return "ANY"
    if "only one subrule" in o or "exactly one" in o:
        return "XOR_ONE"
    if "always" in o and "met" in o:
        return "ALWAYS_MET"
    if "first matching" in o:
        return "FIRST_MATCH"
    return "FIRST_MATCH"


def map_aggregation_rule(v: Any) -> str | None:
    """Map an explicit YAML `aggregation_rule` onto an AGGREGATION_CHOICES value."""
    s = (str(v or "")).strip().lower()
    if not s:
        return None
    if "applicable" in s:           # "applicable_only"
        return "APPLICABLE_ONLY"
    if s in ("any_clean", "any", "any_match"):
        return "ANY"
    if "xor" in s or "exactly_one" in s or "only_one" in s:
        return "XOR_ONE"
    if "first" in s:
        return "FIRST_MATCH"
    if "always" in s:
        return "ALWAYS_MET"
    return None
