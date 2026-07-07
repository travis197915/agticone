"""AI-driven YAML ↔ DB rule reconciliation (SOP "version control").

This is the engine behind the builder's **Compare with YAML** flow. An auditor
uploads/pastes an updated SOP YAML for a SOP; we:

1. **Normalize** the YAML through the *same* planner that the ingestion writer
   uses (:func:`sop_ir.plan.plan_ir`), so the incoming rules are projected into
   exactly the field shape that lives in ``AuditDecision`` rows. This makes the
   comparison apples-to-apples and the join key (``subrule_id``) reliable.
2. **Match** each incoming decision to its DB ``AuditDecision`` by ``subrule_id``
   (the leaf top-level rules are synthesized with ``subrule_id == rule_id`` by
   the planner, so even un-nested rules match).
3. **Compare** each matched pair with an LLM (Claude primary, GPT-4o fallback —
   reusing ``uhc_execution_engine.llm.llm_call``) which returns a *verdict*
   (``IDENTICAL`` / ``AUGMENT`` / ``CONTRADICT``), a human reason, and a
   per-field **proposed merge** (only the fields that should change). A
   deterministic comparator is used when no LLM keys are configured so the
   feature degrades gracefully.
4. **Apply** the auditor-accepted changes to the canonical ``AuditDecision``
   rows (the SOP source of truth), bumping ``AuditDecision.revision`` per rule
   and ``AuditSop.version`` once per batch, and logging every before/after to
   MongoDB (``sop_rule_change_log`` + a ``sop_reconcile_runs`` summary).

Coverage: matched rules (augment/contradict/identical) + YAML-only rules
surfaced as ``NEW`` (the auditor may add them); DB-only rules are surfaced as
``MISSING`` (informational — never auto-deleted).
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from django.db import transaction

from sop_ingestion.models import AuditDecision, AuditSop, AuditStep

logger = logging.getLogger(__name__)


# Fields of an AuditDecision that carry audit logic and may be reconciled.
# (Kept narrow on purpose: codes/goto are derived by the planner; we only let
#  the reconcile touch the human-authored semantic fields.)
RECONCILABLE_FIELDS = (
    "condition_if",
    "condition_and",
    "action_text",
    "output_text",
    "applicable_when",
    "decision_type",
    "is_out_of_scope",
    "tooling_allowed",
)

_VALID_DECISION_TYPES = {c[0] for c in AuditDecision.DECISION_CHOICES}

# Disposition *polarity*. A decision_type change is only a true CONTRADICTION
# when it flips between blocking and passing the claim. Moving to/from a neutral
# type (CONDITIONAL/PEND/SYSTEM) — e.g. a sparse ingested ``BYPASS`` row being
# enriched into a ``CONDITIONAL`` rule — is a refinement, not a reversal.
_DISPOSITION_POLARITY = {
    "DENY": "block", "STOP": "block",
    "ALLOW": "pass", "WAIVE": "pass", "BYPASS": "pass", "REFER": "pass",
    "CONDITIONAL": "neutral", "PEND": "neutral", "SYSTEM": "neutral",
}


def _is_disposition_reversal(cur: Any, inc: Any) -> bool:
    """True only when the disposition flips block↔pass (a real conflict)."""
    pc = _DISPOSITION_POLARITY.get(str(cur or "").upper().strip())
    pi = _DISPOSITION_POLARITY.get(str(inc or "").upper().strip())
    if not pc or not pi:
        return False
    return {pc, pi} == {"block", "pass"}


# ── Mongo handle (mirrors sop_ir.persist._mongo_db) ──────────────────────────
def _mongo_db():
    try:
        from pymongo import MongoClient

        from sop_backend.db_config import is_prod, mongo_uri_from_env

        if not (os.environ.get("MONGO_HOST") or (is_prod() and os.environ.get("MONGO_URI"))):
            return None
        database = os.environ.get("MONGO_DATABASE", "sop_ingestion")
        client = MongoClient(mongo_uri_from_env(), serverSelectionTimeoutMS=10000)
        return client[database]
    except Exception as exc:  # pragma: no cover - infra dependent
        logger.warning("rule_reconcile: Mongo unavailable (%s)", exc)
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── YAML → normalized incoming decisions ─────────────────────────────────────
def _flatten_plan(plan: list[dict]) -> dict[str, dict]:
    """Flatten a ``plan_ir`` result into ``{subrule_id: incoming_decision}``.

    Each incoming decision carries the normalized AuditDecision fields plus the
    owning step's ``step_number`` / ``rule_id`` so a ``NEW`` rule can be created
    under the right step.
    """
    out: dict[str, dict] = {}

    def _walk(node: dict, step_number: int, rule_id: str):
        sid = (node.get("subrule_id") or "").strip()
        rec = {
            "subrule_id": sid,
            "step_number": step_number,
            "step_rule_id": rule_id,
            "table_name": node.get("table_name", ""),
            "condition_if": node.get("condition_if", "") or "",
            "condition_and": node.get("condition_and", "") or "",
            "action_text": node.get("action_text", "") or "",
            "output_text": node.get("output_text", "") or "",
            "applicable_when": node.get("applicable_when", "") or "",
            "decision_type": node.get("decision_type", "") or "",
            "is_out_of_scope": bool(node.get("is_out_of_scope", False)),
            "tooling_allowed": bool(node.get("tooling_allowed", True)),
        }
        if sid:
            out[sid] = rec
        for child in node.get("children", []) or []:
            _walk(child, step_number, rule_id)

    for step in plan:
        sn = step.get("step_number")
        rid = step.get("rule_id", "")
        for child in step.get("children", []) or []:
            _walk(child, sn, rid)
    return out


def _flatten_plan_list(plan: list[dict]) -> list[dict]:
    """Flatten a ``plan_ir`` result into an ordered list of incoming decisions.

    Unlike :func:`_flatten_plan` (keyed by ``subrule_id``), this keeps EVERY
    decision — including ones the source left without an id — so the
    content-based matcher can pair them by meaning, not just by id.
    """
    out: list[dict] = []

    def _walk(node: dict, step_number, rule_id: str):
        out.append({
            "subrule_id": (node.get("subrule_id") or "").strip(),
            "step_number": step_number,
            "step_rule_id": rule_id,
            "table_name": node.get("table_name", ""),
            "condition_if": node.get("condition_if", "") or "",
            "condition_and": node.get("condition_and", "") or "",
            "action_text": node.get("action_text", "") or "",
            "output_text": node.get("output_text", "") or "",
            "applicable_when": node.get("applicable_when", "") or "",
            "decision_type": node.get("decision_type", "") or "",
            "is_out_of_scope": bool(node.get("is_out_of_scope", False)),
            "tooling_allowed": bool(node.get("tooling_allowed", True)),
        })
        for child in node.get("children", []) or []:
            _walk(child, step_number, rule_id)

    for step in plan:
        sn = step.get("step_number")
        rid = step.get("rule_id", "")
        for child in step.get("children", []) or []:
            _walk(child, sn, rid)
    return out


def _parse_plan(yaml_text: str) -> list[dict]:
    import yaml as _yaml

    from sop_ir.plan import plan_ir
    from sop_ir.schema import SopIR

    doc = _yaml.safe_load(yaml_text)
    if not isinstance(doc, dict):
        raise ValueError("SOP YAML root must be a mapping with sop_metadata/sop_rules.")
    ir = SopIR.from_yaml_doc(doc)
    return plan_ir(ir)


def parse_yaml_to_incoming(yaml_text: str) -> dict[str, dict]:
    """Parse + normalize a SOP YAML into ``{subrule_id: incoming_decision}``."""
    return _flatten_plan(_parse_plan(yaml_text))


def parse_yaml_rules(yaml_text: str) -> list[dict]:
    """Parse + normalize a SOP YAML into an ordered list of incoming rules."""
    return _flatten_plan_list(_parse_plan(yaml_text))


# ── content-based matching (robust to id-scheme / structure differences) ──────
import math as _math
import re as _re

# 9-10 digit identifiers (provider TINs / NPIs) are globally unique gate keys —
# the strongest possible signal that two rows are the SAME rule.
_KEYID_RE = _re.compile(r"\b\d{9,10}\b")
_EMBED_THRESHOLD = 0.80
_LEXICAL_THRESHOLD = 0.70


def _rule_blob(fields: dict) -> str:
    return " ".join(str(fields.get(k) or "") for k in (
        "condition_if", "condition_and", "action_text", "output_text",
        "applicable_when",
    )).strip()


def _key_ids(text: str) -> set[str]:
    return set(_KEYID_RE.findall(text or ""))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = _math.sqrt(sum(x * x for x in a))
    nb = _math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _embed(texts: list[str], cfg) -> list[list[float]] | None:
    """Embed texts with OpenAI (text-embedding-3-small); None when unavailable."""
    key = getattr(cfg, "openai_api_key", "")
    if not key or not texts:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        vectors: list[list[float]] = []
        for i in range(0, len(texts), 256):
            chunk = [(t[:6000] or " ") for t in texts[i:i + 256]]
            resp = client.embeddings.create(
                model="text-embedding-3-small", input=chunk)
            vectors.extend(d.embedding for d in resp.data)
        return vectors
    except Exception as exc:  # pragma: no cover - provider/infra dependent
        logger.warning("rule_reconcile: embedding failed (%s) — lexical fallback", exc)
        return None


def _match_rules(incoming: list[dict], db_list: list[AuditDecision], cfg
                 ) -> list[tuple[Any, Any]]:
    """Pair YAML rules to DB rules by content, not id scheme.

    Cascade: (1) exact ``subrule_id``, (2) shared unique identifier (TIN/NPI),
    (3) semantic embedding similarity, (4) lexical similarity fallback. Returns
    a list of ``(db_decision|None, incoming|None)`` covering matched pairs,
    DB-only rows (``incoming None`` → MISSING) and YAML-only rows
    (``db None`` → NEW). Assignment is one-to-one.
    """
    n_in, n_db = len(incoming), len(db_list)
    in_text = [_norm(_rule_blob(r)) for r in incoming]
    db_text = [_norm(_rule_blob(_decision_fields(d))) for d in db_list]
    in_ids = [_key_ids(t) for t in in_text]
    db_ids = [_key_ids(t) for t in db_text]

    matched_db: set[int] = set()
    matched_in: set[int] = set()
    pairs: list[tuple[int, int]] = []

    # 1) exact subrule_id
    db_by_sid: dict[str, int] = {}
    for j, d in enumerate(db_list):
        s = (d.subrule_id or "").strip()
        if s and s not in db_by_sid:
            db_by_sid[s] = j
    for i, r in enumerate(incoming):
        s = r.get("subrule_id", "")
        j = db_by_sid.get(s)
        if s and j is not None and j not in matched_db:
            pairs.append((j, i)); matched_db.add(j); matched_in.add(i)

    # 2) shared unique identifier (provider TIN/NPI) — exact gate-key match
    for i in range(n_in):
        if i in matched_in or not in_ids[i]:
            continue
        for j in range(n_db):
            if j in matched_db or not db_ids[j]:
                continue
            if in_ids[i] & db_ids[j]:
                pairs.append((j, i)); matched_db.add(j); matched_in.add(i)
                break

    # 3/4) semantic (embeddings) then lexical for whatever is still unmatched.
    # A small bonus when the YAML rule and the DB rule sit on the SAME step
    # number breaks ties toward the structurally-correct counterpart (e.g. the
    # YAML's RULE-002 body → the DB's step-2 rule even when wording diverges).
    rem_in = [i for i in range(n_in) if i not in matched_in]
    rem_db = [j for j in range(n_db) if j not in matched_db]
    in_step = [r.get("step_number") for r in incoming]
    db_step = [d.step.step_number for d in db_list]
    if rem_in and rem_db:
        scored: list[tuple[float, int, int]] = []

        def _bonus(i: int, j: int) -> float:
            return 0.06 if (in_step[i] is not None and in_step[i] == db_step[j]) else 0.0

        embs = _embed([in_text[i] for i in rem_in] + [db_text[j] for j in rem_db], cfg)
        if embs and len(embs) == len(rem_in) + len(rem_db):
            in_emb = embs[:len(rem_in)]
            db_emb = embs[len(rem_in):]
            for a, i in enumerate(rem_in):
                for b, j in enumerate(rem_db):
                    sc = _cosine(in_emb[a], db_emb[b]) + _bonus(i, j)
                    if sc >= _EMBED_THRESHOLD:
                        scored.append((sc, i, j))
        else:
            import difflib
            for i in rem_in:
                for j in rem_db:
                    if not in_text[i] or not db_text[j]:
                        continue
                    sc = difflib.SequenceMatcher(None, in_text[i], db_text[j]).ratio() \
                        + _bonus(i, j)
                    if sc >= _LEXICAL_THRESHOLD:
                        scored.append((sc, i, j))
        scored.sort(reverse=True)
        for sc, i, j in scored:
            if i in matched_in or j in matched_db:
                continue
            pairs.append((j, i)); matched_in.add(i); matched_db.add(j)

    result: list[tuple[Any, Any]] = [(db_list[j], incoming[i]) for j, i in pairs]
    result += [(db_list[j], None) for j in range(n_db) if j not in matched_db]
    result += [(None, incoming[i]) for i in range(n_in) if i not in matched_in]
    return result


# ── DB snapshot ───────────────────────────────────────────────────────────────
def _decision_fields(dec: AuditDecision) -> dict[str, Any]:
    return {f: getattr(dec, f) for f in RECONCILABLE_FIELDS}


def _rule_key(sop_id: int, step_number: int, row_index: int) -> str:
    return f"step:{sop_id}:{step_number}:{row_index}"


def _db_decisions(sop: AuditSop) -> list[AuditDecision]:
    return list(
        AuditDecision.objects.filter(step__sop=sop)
        .select_related("step")
        .order_by("step__step_number", "depth", "row_index")
    )


# ── comparison ────────────────────────────────────────────────────────────────
def _norm(v: Any) -> str:
    return " ".join(str(v or "").split()).strip().lower()


def _deterministic_compare(current: dict, incoming: dict) -> dict:
    """Fallback comparator when no LLM is configured.

    AUGMENT when the YAML strictly adds text to a field (superset); CONTRADICT
    when a field's meaning changes incompatibly (different decision_type,
    out-of-scope flip, or non-superset text change); IDENTICAL otherwise.
    """
    proposed: dict[str, Any] = {}
    contradict = False
    for f in RECONCILABLE_FIELDS:
        cur, inc = current.get(f), incoming.get(f)
        if f == "tooling_allowed":
            if bool(cur) != bool(inc):
                proposed[f] = bool(inc)
            continue
        if f == "is_out_of_scope":
            # Only a *reversal* of scope conflicts (in-scope → out-of-scope or
            # vice versa). Equal values are skipped above by the bool check.
            if bool(cur) != bool(inc):
                proposed[f] = bool(inc)
                contradict = True
            continue
        if f == "decision_type":
            if inc and _norm(inc) != _norm(cur):
                proposed[f] = inc
                # A type change is a conflict ONLY when it flips block↔pass.
                if _is_disposition_reversal(cur, inc):
                    contradict = True
            continue
        cn, inn = _norm(cur), _norm(inc)
        if cn == inn:
            continue
        if not inc:
            continue
        # Text fields (condition/action/output/applicable_when): a rewrite is
        # treated as enrichment, not a conflict. Lexical comparison can't
        # reliably tell a genuine reversal from sparse-ingested text vs. fuller
        # YAML prose, so we default to AUGMENT and let the LLM (when available)
        # or the auditor flag true contradictions. Only disposition/scope
        # reversals (handled above) flip the verdict deterministically.
        proposed[f] = inc
    if not proposed:
        return {"verdict": "IDENTICAL", "reason": "No material difference detected.",
                "proposed": {}}
    if contradict:
        return {
            "verdict": "CONTRADICT",
            "reason": "YAML reverses the rule's outcome (block↔pass or scope flip) "
                      "or rewrites a condition/action in a conflicting way.",
            "proposed": proposed,
        }
    return {
        "verdict": "AUGMENT",
        "reason": "YAML adds richer context/detail (or fills a sparse ingested "
                  "field) without reversing the rule's outcome.",
        "proposed": proposed,
    }


def _ai_compare(current: dict, incoming: dict, *, subrule_id: str) -> dict:
    """LLM verdict + per-field proposed merge, with a deterministic fallback.

    Short-circuit: when the deterministic comparator finds NO field difference
    the rules are identical, so we skip the LLM entirely. Only rules that
    actually diverge are sent to the model — this keeps an analyze pass over a
    large SOP fast (one call per *changed* rule, not per rule).
    """
    fallback = _deterministic_compare(current, incoming)
    if fallback["verdict"] == "IDENTICAL":
        return fallback
    try:
        from uhc_execution_engine.config import get_config
        from uhc_execution_engine.llm import llm_call
    except Exception:
        return fallback

    try:
        cfg = get_config()
    except Exception:
        return fallback
    if not (getattr(cfg, "anthropic_api_key", "") or getattr(cfg, "openai_api_key", "")):
        return fallback

    prompt = _build_compare_prompt(current, incoming, subrule_id)
    try:
        data, _meta = llm_call(
            cfg, prompt,
            agent_name="rule_reconcile_compare",
            stage="yaml_rule_reconcile",
            fallback=fallback,
            provider="anthropic" if cfg.anthropic_api_key else "openai",
            expected_type=dict,
            required_keys=["verdict", "reason", "proposed"],
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("rule_reconcile _ai_compare failed: %s", exc)
        return fallback

    verdict = str(data.get("verdict", "")).upper().strip()
    if verdict not in ("IDENTICAL", "AUGMENT", "CONTRADICT"):
        return fallback
    proposed_raw = data.get("proposed") or {}
    proposed = _sanitize_proposed(proposed_raw, current)
    if verdict == "IDENTICAL":
        proposed = {}
    return {
        "verdict": verdict,
        "reason": str(data.get("reason") or "")[:600],
        "proposed": proposed,
    }


def _sanitize_proposed(proposed: dict, current: dict) -> dict[str, Any]:
    """Keep only whitelisted fields whose value actually differs + is valid."""
    clean: dict[str, Any] = {}
    if not isinstance(proposed, dict):
        return clean
    for f in RECONCILABLE_FIELDS:
        if f not in proposed:
            continue
        val = proposed[f]
        if f in ("is_out_of_scope", "tooling_allowed"):
            val = bool(val)
            if val != bool(current.get(f)):
                clean[f] = val
        elif f == "decision_type":
            v = str(val or "").upper().strip()
            if v in _VALID_DECISION_TYPES and v != current.get(f):
                clean[f] = v
        else:
            v = str(val if val is not None else "")
            if _norm(v) != _norm(current.get(f)):
                clean[f] = v
    return clean


def _build_compare_prompt(current: dict, incoming: dict, subrule_id: str) -> str:
    import json
    return (
        "You are a senior claims-audit SOP editor performing version control on "
        "a single audit rule. You are given the CURRENT rule (as stored in the "
        "database, often ingested from a PDF/HTML doc and therefore SPARSE — it "
        "may hold only an identifier, a name, or a one-word disposition) and an "
        "INCOMING version of the same rule (hand-authored in an updated SOP "
        f"YAML, usually richer). Rule id: {subrule_id or '(unnamed)'}.\n\n"
        "These two describe the SAME underlying rule (they were matched by id / "
        "shared TIN-NPI / semantic similarity). Your job is to judge whether the "
        "INCOMING text AGREES with the current rule's intent (just says it more "
        "fully) or genuinely CONFLICTS with it.\n\n"
        "Return a JSON object with EXACTLY these keys:\n"
        '  "verdict": one of:\n'
        '    - "IDENTICAL": no material difference.\n'
        '    - "AUGMENT": the incoming version adds detail, fills empty/sparse '
        "fields, clarifies conditions, or spells out Met/Not-Met text WITHOUT "
        "reversing the outcome. THIS IS THE DEFAULT when the intent is "
        "preserved.\n"
        '    - "CONTRADICT": the incoming version REVERSES the outcome — i.e. it '
        "flips a deny into a pay/allow (or vice versa), flips in-scope ↔ "
        "out-of-scope, negates the condition, or changes a threshold / amount / "
        "code / id to a genuinely conflicting value.\n\n"
        "CRITICAL RULES for the verdict:\n"
        "  1. A change in `decision_type` is NOT by itself a contradiction. "
        "Moving between a neutral type (CONDITIONAL/PEND/SYSTEM) and a "
        "pass-type (BYPASS/ALLOW/WAIVE/REFER) — e.g. BYPASS → CONDITIONAL — is a "
        "REFINEMENT → AUGMENT. Only a flip between blocking (DENY/STOP) and "
        "passing (ALLOW/WAIVE/BYPASS/REFER) is a CONTRADICT.\n"
        "  2. If the CURRENT rule only lists data (e.g. a provider TIN + name) "
        "and the INCOMING rule describes the SAME entity with full instructions "
        "that point the same way (e.g. 'exclude this provider from "
        "cross-billing'), that is AUGMENT — the YAML is the fuller statement of "
        "the same rule.\n"
        "  3. Reserve CONTRADICT for real disagreements an auditor must "
        "reconcile (opposite disposition, reversed scope, conflicting "
        "number/code/id).\n\n"
        '  "reason": one or two sentences, plain English, explaining the verdict '
        "so an auditor can decide whether to accept it.\n"
        '  "proposed": an object with ONLY the fields that should change if '
        f"accepted, mapped to the new merged value. Valid fields: "
        f"{', '.join(RECONCILABLE_FIELDS)}. For AUGMENT, MERGE (keep the current "
        "meaning, fold in the incoming detail). For CONTRADICT, propose the "
        "incoming value so the auditor sees the resolution. For IDENTICAL, "
        'return an empty object. "decision_type" must be one of '
        f"{sorted(_VALID_DECISION_TYPES)}.\n\n"
        "CURRENT RULE:\n" + json.dumps(current, default=str, indent=2) + "\n\n"
        "INCOMING RULE (from YAML):\n" + json.dumps(incoming, default=str, indent=2)
    )


# ── public: analyze ───────────────────────────────────────────────────────────
def _noop_progress(processed: int, total: int, phase: str) -> None:  # pragma: no cover
    pass


def analyze(sop: AuditSop, yaml_text: str, *, source: str = "pasted",
            progress_cb=None) -> dict:
    """Compare an uploaded YAML against the SOP's current rules.

    Returns ``{reconcile_id, sop_id, sop_version, source, counts, findings}``.
    The findings list is what the UI renders as the diff table; the same list
    is echoed back to :func:`apply` for the accepted rows.

    ``progress_cb(processed, total, phase)`` is called as each DB rule is
    compared so a Celery task can stream progress to the UI. It is optional —
    when omitted the analysis runs exactly as before.
    """
    progress = progress_cb or _noop_progress
    incoming = parse_yaml_rules(yaml_text)
    decisions = _db_decisions(sop)
    progress(0, len(decisions), "loading")

    # Content-based pairing (robust to id-scheme / structure differences between
    # the hand-authored YAML and whatever the ingestion door produced).
    try:
        from uhc_execution_engine.config import get_config
        _cfg = get_config()
    except Exception:
        _cfg = None
    progress(0, len(decisions), "matching")
    pairs = _match_rules(incoming, decisions, _cfg)

    findings: list[dict] = []
    total = len(pairs)
    for _i, (dec, inc) in enumerate(pairs):
        progress(_i + 1, total, "comparing")

        # DB-only → MISSING (informational; never auto-deleted).
        if inc is None:
            cur = _decision_fields(dec)
            findings.append({
                "verdict": "MISSING",
                "rule_key": _rule_key(sop.id, dec.step.step_number, dec.row_index),
                "decision_id": dec.id,
                "subrule_id": (dec.subrule_id or "").strip(),
                "yaml_subrule_id": "",
                "step_number": dec.step.step_number,
                "table_name": dec.table_name,
                "revision": dec.revision,
                "reason": "Present in the database but no matching rule in the "
                          "uploaded YAML. Not changed.",
                "current": _jsonable(cur),
                "incoming": None,
                "proposed": {},
                "fields_changed": [],
            })
            continue

        inc_fields = {k: inc.get(k) for k in RECONCILABLE_FIELDS}

        # YAML-only → NEW (auditor may add it under its step).
        if dec is None:
            findings.append({
                "verdict": "NEW",
                "rule_key": None,
                "decision_id": None,
                "subrule_id": inc.get("subrule_id", ""),
                "yaml_subrule_id": inc.get("subrule_id", ""),
                "step_number": inc.get("step_number"),
                "step_rule_id": inc.get("step_rule_id"),
                "table_name": inc.get("table_name", ""),
                "revision": 0,
                "reason": "Present in the uploaded YAML but no matching rule in "
                          "the database. Accept to add it as a new rule under its "
                          "step.",
                "current": None,
                "incoming": _jsonable(inc_fields),
                "proposed": _jsonable(inc_fields),
                "fields_changed": list(RECONCILABLE_FIELDS),
            })
            continue

        # Matched pair → AI decides augment / contradict / identical.
        cur = _decision_fields(dec)
        cmp = _ai_compare(cur, inc_fields,
                          subrule_id=inc.get("subrule_id") or dec.subrule_id)
        proposed = cmp["proposed"]
        findings.append({
            "verdict": cmp["verdict"],
            "rule_key": _rule_key(sop.id, dec.step.step_number, dec.row_index),
            "decision_id": dec.id,
            "subrule_id": (dec.subrule_id or "").strip(),
            "yaml_subrule_id": inc.get("subrule_id", ""),
            "step_number": dec.step.step_number,
            "table_name": dec.table_name,
            "revision": dec.revision,
            "reason": cmp["reason"],
            "current": _jsonable(cur),
            "incoming": _jsonable(inc_fields),
            "proposed": _jsonable(proposed),
            "fields_changed": sorted(proposed.keys()),
        })

    order = {"CONTRADICT": 0, "AUGMENT": 1, "NEW": 2, "IDENTICAL": 3, "MISSING": 4}
    findings.sort(key=lambda f: (order.get(f["verdict"], 9),
                                 f.get("step_number") or 0,
                                 f.get("subrule_id") or ""))

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["verdict"]] = counts.get(f["verdict"], 0) + 1

    reconcile_id = uuid.uuid4().hex
    result = {
        "reconcile_id": reconcile_id,
        "sop_id": sop.id,
        "sop_title": sop.title,
        "sop_version": sop.version,
        "source": source,
        "counts": counts,
        "findings": findings,
        "analyzed_at": _now_iso(),
    }

    # Best-effort: archive the analysis run so the apply step (and history) can
    # reference it. Never blocks the response.
    db = _mongo_db()
    if db is not None:
        try:
            db["sop_reconcile_runs"].replace_one(
                {"_id": reconcile_id},
                {"_id": reconcile_id, **result},
                upsert=True,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("rule_reconcile: run archive failed (%s)", exc)

    return result


# ── public: apply ─────────────────────────────────────────────────────────────
def apply(sop: AuditSop, accepted: list[dict], *,
          user: str = "system", yaml_source: str = "pasted",
          reconcile_id: str = "") -> dict:
    """Apply auditor-accepted findings to the canonical SOP rules.

    ``accepted`` is the subset of :func:`analyze` findings the auditor approved.
    Each item must carry at least ``verdict`` + (``decision_id`` for updates /
    ``step_number`` + ``incoming`` for NEW) + ``proposed``. Bumps per-rule
    ``revision`` and the SOP ``version`` once, and logs every change to Mongo.
    """
    if not accepted:
        return {"sop_id": sop.id, "version": sop.version, "applied": [],
                "skipped": [], "message": "No rules accepted."}

    log_entries: list[dict] = []
    applied: list[dict] = []
    skipped: list[dict] = []
    batch_id = uuid.uuid4().hex

    with transaction.atomic():
        sop_locked = AuditSop.objects.select_for_update().get(pk=sop.id)
        new_version = sop_locked.version + 1

        for item in accepted:
            verdict = str(item.get("verdict", "")).upper()
            proposed = _sanitize_proposed(item.get("proposed") or {}, item.get("current") or {}) \
                if item.get("current") else (item.get("proposed") or {})

            if verdict in ("AUGMENT", "CONTRADICT"):
                dec = _resolve_decision(sop_locked, item)
                if dec is None:
                    skipped.append({"rule_key": item.get("rule_key"),
                                    "reason": "decision not found"})
                    continue
                before = _decision_fields(dec)
                clean = _sanitize_proposed(item.get("proposed") or {}, before)
                if not clean:
                    skipped.append({"rule_key": item.get("rule_key"),
                                    "reason": "no applicable field changes"})
                    continue
                for f, v in clean.items():
                    setattr(dec, f, v)
                dec.revision = (dec.revision or 1) + 1
                dec.save(update_fields=list(clean.keys()) + ["revision"])
                after = _decision_fields(dec)
                rule_key = _rule_key(sop_locked.id, dec.step.step_number, dec.row_index)
                applied.append({"rule_key": rule_key, "decision_id": dec.id,
                                "verdict": verdict,
                                "fields_changed": sorted(clean.keys()),
                                "revision": dec.revision})
                log_entries.append(_log_doc(
                    sop_locked, batch_id, verdict, rule_key, dec,
                    before, after, sorted(clean.keys()), item.get("reason", ""),
                    user, yaml_source, reconcile_id, new_version))

            elif verdict == "NEW":
                dec = _create_new_decision(sop_locked, item)
                if dec is None:
                    skipped.append({"subrule_id": item.get("subrule_id"),
                                    "reason": "could not resolve target step"})
                    continue
                after = _decision_fields(dec)
                rule_key = _rule_key(sop_locked.id, dec.step.step_number, dec.row_index)
                applied.append({"rule_key": rule_key, "decision_id": dec.id,
                                "verdict": "NEW", "fields_changed": list(after.keys()),
                                "revision": dec.revision})
                log_entries.append(_log_doc(
                    sop_locked, batch_id, "NEW", rule_key, dec,
                    None, after, list(after.keys()), item.get("reason", ""),
                    user, yaml_source, reconcile_id, new_version))
            else:
                skipped.append({"rule_key": item.get("rule_key"),
                                "reason": f"verdict {verdict} is not applicable"})

        if applied:
            sop_locked.version = new_version
            sop_locked.save(update_fields=["version", "updated_at"])

    # Mongo audit log (best-effort, outside the txn).
    if applied:
        _write_log(sop, batch_id, log_entries, user, yaml_source,
                   reconcile_id, new_version, applied, skipped)

    return {
        "sop_id": sop.id,
        "version": sop.version + 1 if applied else sop.version,
        "batch_id": batch_id,
        "applied": applied,
        "skipped": skipped,
        "log_count": len(log_entries),
    }


def _resolve_decision(sop: AuditSop, item: dict) -> Optional[AuditDecision]:
    dec_id = item.get("decision_id")
    qs = AuditDecision.objects.select_related("step").filter(step__sop=sop)
    if dec_id:
        return qs.filter(id=dec_id).first()
    sid = (item.get("subrule_id") or "").strip()
    if sid:
        return qs.filter(subrule_id=sid).first()
    return None


def _create_new_decision(sop: AuditSop, item: dict) -> Optional[AuditDecision]:
    step = None
    sn = item.get("step_number")
    if sn is not None:
        step = AuditStep.objects.filter(sop=sop, step_number=sn).first()
    if step is None and item.get("step_rule_id"):
        step = AuditStep.objects.filter(sop=sop, yaml_rule_id=item["step_rule_id"]).first()
    if step is None:
        return None
    inc = item.get("incoming") or item.get("proposed") or {}
    next_row = (AuditDecision.objects.filter(step=step)
                .order_by("-row_index").values_list("row_index", flat=True).first())
    row_index = (next_row + 1) if next_row is not None else 0
    dtype = str(inc.get("decision_type") or "CONDITIONAL").upper()
    if dtype not in _VALID_DECISION_TYPES:
        dtype = "CONDITIONAL"
    return AuditDecision.objects.create(
        step=step, parent=None, depth=0,
        subrule_id=(item.get("subrule_id") or "")[:64],
        table_name=(inc.get("table_name") or "")[:256],
        row_index=row_index,
        condition_if=inc.get("condition_if", "") or "",
        condition_and=inc.get("condition_and", "") or "",
        action_text=inc.get("action_text", "") or "",
        output_text=inc.get("output_text", "") or "",
        applicable_when=inc.get("applicable_when", "") or "",
        decision_type=dtype,
        is_out_of_scope=bool(inc.get("is_out_of_scope", False)),
        tooling_allowed=bool(inc.get("tooling_allowed", True)),
        revision=1,
    )


# ── Mongo logging ─────────────────────────────────────────────────────────────
def _jsonable(d: Any) -> Any:
    """Decision-type/bool/text values are already JSON-safe; pass through."""
    return d


def _log_doc(sop, batch_id, verdict, rule_key, dec, before, after,
             fields_changed, reason, user, yaml_source, reconcile_id,
             sop_version) -> dict:
    return {
        "_id": uuid.uuid4().hex,
        "batch_id": batch_id,
        "reconcile_id": reconcile_id,
        "sop_id": sop.id,
        "sop_title": sop.title,
        "sop_version": sop_version,
        "rule_key": rule_key,
        "decision_id": dec.id,
        "subrule_id": dec.subrule_id,
        "step_number": dec.step.step_number,
        "rule_revision": dec.revision,
        "verdict": verdict,
        "reason": reason,
        "fields_changed": fields_changed,
        "before": before,
        "after": after,
        "yaml_source": yaml_source,
        "user": user,
        "ts": _now_iso(),
    }


def _write_log(sop, batch_id, log_entries, user, yaml_source, reconcile_id,
               sop_version, applied, skipped) -> None:
    db = _mongo_db()
    if db is None:
        logger.info("rule_reconcile: Mongo not configured; skipped change log "
                    "(%d entries)", len(log_entries))
        return
    try:
        if log_entries:
            db["sop_rule_change_log"].insert_many(log_entries)
        db["sop_reconcile_batches"].replace_one(
            {"_id": batch_id},
            {
                "_id": batch_id,
                "reconcile_id": reconcile_id,
                "sop_id": sop.id,
                "sop_title": sop.title,
                "sop_version": sop_version,
                "user": user,
                "yaml_source": yaml_source,
                "applied_count": len(applied),
                "skipped_count": len(skipped),
                "applied": applied,
                "skipped": skipped,
                "ts": _now_iso(),
            },
            upsert=True,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("rule_reconcile: change-log write failed (%s)", exc)


# ── public: version / history ─────────────────────────────────────────────────
def version_info(sop: AuditSop, *, limit: int = 25) -> dict:
    """Current SOP version + recent change-log entries (from Mongo)."""
    changes: list[dict] = []
    db = _mongo_db()
    if db is not None:
        try:
            cur = (db["sop_rule_change_log"]
                   .find({"sop_id": sop.id}, {"before": 0, "after": 0})
                   .sort("ts", -1).limit(limit))
            changes = list(cur)
        except Exception as exc:  # pragma: no cover
            logger.warning("rule_reconcile: history read failed (%s)", exc)
    return {
        "sop_id": sop.id,
        "sop_title": sop.title,
        "version": sop.version,
        "recent_changes": changes,
    }
