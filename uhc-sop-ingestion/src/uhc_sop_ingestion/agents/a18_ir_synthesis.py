"""
a18_ir_synthesis.py — Canonical SOP-IR Synthesis (maker + checker)

This stage closes the fidelity gap between the YAML door and the HTML/PDF door.
The flat ``pg_step_writer`` (a11) writes ``LEAF`` rows and drops the routing
metadata the execution engine relies on (``aggregation``, nested subrules,
``goto_step``, ``applicable_when``, out-of-scope). Here we synthesize the same
canonical IR a hand-authored YAML produces, so that — once persisted through the
shared ``sop_ir.persist.persist_ir`` gate — an ingested SOP routes identically.

Two agents, mirroring a16's Redis-blackboard pattern (namespace ``sop:ir:{job}``):

  * ``ir_maker``   — builds a deterministic draft IR from the enriched steps
                     (always succeeds), then runs an LLM routing-enricher that
                     infers ONLY the routing fields the flat writer drops, using
                     the ``RuleNode`` JSON schema as the output contract. The LLM
                     never rewrites verbatim rule text — only routing.
  * ``ir_checker`` — runs ``sop_ir.validate.validate_ir`` with a bounded,
                     deterministic repair loop; records validation status on the
                     blackboard and in state. Never silently defaults: residual
                     issues are flagged.

The stage writes ``state["sop_ir"]`` (pure JSON data — no DB write here). The
authoritative relational write happens later in ``pipeline_runner`` via
``persist_ir`` (Django ORM context), behind the ``SOP_IR_PERSIST`` flag.

``sop_ir`` (the top-level package) is imported defensively so the standalone
``sop-ingest`` CLI (where the Django project root may be off ``sys.path``) still
runs the deterministic draft and degrades gracefully.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

from .a07_enrich import _llm_call

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

log = logging.getLogger(__name__)

_CONTEXT_TTL_SECONDS = 24 * 3600


# ── optional sop_ir import (Django project root) ──────────────────────────────
def _load_sop_ir():
    """Return the (SopIR, validate_ir) pair, or (None, None) if unavailable."""
    try:
        from sop_ir.schema import RuleNode, SopIR  # noqa: F401
        from sop_ir.validate import validate_ir
        return SopIR, validate_ir
    except Exception as exc:  # pragma: no cover - standalone CLI without project root
        log.warning("a18: sop_ir package unavailable (%s) — running draft-only", exc)
        return None, None


# ── Redis blackboard (sop:ir namespace) ───────────────────────────────────────
def _redis(cfg: "PipelineConfig"):
    from ..config import get_redis
    return get_redis(cfg)


def _ir_key(job_id: str, section: str) -> str:
    return f"sop:ir:{job_id}:{section}"


def ctx_write(cfg, job_id: str, section: str, payload: Any) -> None:
    try:
        r = _redis(cfg)
        r.set(_ir_key(job_id, section), json.dumps(payload, default=str))
        r.expire(_ir_key(job_id, section), _CONTEXT_TTL_SECONDS)
    except Exception as exc:
        log.warning("a18 ctx_write[%s] failed: %s", section, exc)


def ctx_read(cfg, job_id: str, section: str, default=None):
    try:
        r = _redis(cfg)
        raw = r.get(_ir_key(job_id, section))
        return json.loads(raw) if raw else default
    except Exception as exc:
        log.warning("a18 ctx_read[%s] failed: %s", section, exc)
        return default


def ctx_audit_trail(cfg, job_id: str, agent: str, status: str, detail: str = "") -> None:
    try:
        r = _redis(cfg)
        key = _ir_key(job_id, "audit_trail")
        r.rpush(key, json.dumps({"agent": agent, "status": status, "detail": detail[:300]}))
        r.expire(key, _CONTEXT_TTL_SECONDS)
    except Exception:
        pass


# ── helpers ───────────────────────────────────────────────────────────────────
def _txt(v: Any, maxlen: int | None = None) -> str:
    s = "" if v is None else str(v)
    if "\x00" in s:
        s = s.replace("\x00", "")
    return s[:maxlen] if maxlen else s


def _int_or_none(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _step_num(step: dict) -> Optional[int]:
    return _int_or_none(step.get("step_number", step.get("number")))


def _row_codes(row: dict) -> dict:
    return {
        "eob": list(row.get("eob_codes") or []),
        "ex": list(row.get("ex_codes") or []),
        "denial": list(row.get("denial_codes") or []),
        "sysact": list(row.get("system_actions") or []),
    }


# ── recursive decision-row → Subrule mapper ──────────────────────────────────
def _row_to_subrule(parent_id: str, row: dict, ridx: int) -> dict:
    """Map one decision row into a SopIR ``Subrule`` dict, RECURSING into nested
    children so sub-sub-rules survive into the Pydantic IR.

    Nested children are read from ``row["subrules"]`` or ``row["children"]``
    (the keys the new PDF/HTML synthesis emits). Field names mirror exactly what
    ``build_draft_ir`` historically read so HTML and PDF drafts stay identical.
    """
    cond_if = _txt(row.get("condition_if", row.get("if", "")))
    cond_and = _txt(row.get("condition_and", row.get("and", "")))
    action = _txt(row.get("action", row.get("then", "")))
    goto = _int_or_none(row.get("skip_to_step") or row.get("goto_step"))
    nav = {"op": "goto", "step_number": goto} if goto is not None else None
    subrule_id = _txt(row.get("subrule_id", "")) or f"{parent_id}-{ridx + 1:03d}"

    children_raw = row.get("subrules") or row.get("children") or []
    children: list[dict] = []
    for cidx, child in enumerate(children_raw):
        if isinstance(child, dict):
            children.append(_row_to_subrule(subrule_id, child, cidx))

    return {
        "subrule_id": subrule_id,
        "table_name": _txt(row.get("table_name", "")),
        "description": cond_if,
        "conditions": [c for c in [cond_and] if c],
        "actions": [a for a in [action] if a],
        "output": _txt(row.get("output_text", "")),
        "applicable_when": _txt(row.get("applicable_when", "")),
        "is_out_of_scope": bool(row.get("is_out_of_scope")),
        "navigation": nav,
        "subrules": children,
    }


# ── deterministic draft ───────────────────────────────────────────────────────
def build_draft_ir(state: "PipelineState") -> dict:
    """Build a SopIR-shaped dict from enriched steps. Always succeeds.

    Maps the same step/decision_row fields ``pg_step_writer`` reads, but into
    the nested IR shape so ``persist_ir`` can synthesize routing-complete rows
    (leaf synthesis, goto extraction, APPLICABLE_ONLY forcing, OOS propagation)."""
    meta = state.get("metadata") or {}
    steps = state.get("enriched_steps") or state.get("steps") or []

    metadata = {
        "document_title": _txt(meta.get("title", "")),
        "document_version": _txt(meta.get("revision_date", "")),
        "effective_from": _txt(meta.get("effective_date", "")) or None,
        "processing_mode": "agentic_ingestion",
        "provider": _txt(meta.get("platform", "")),
        "source_file": _txt(state.get("current_url", "")),
    }

    rules: list[dict] = []
    ordered = sorted(
        [s for s in steps if _step_num(s) is not None],
        key=lambda s: _step_num(s),
    )
    for s in ordered:
        num = _step_num(s)
        rule_id = _txt(s.get("yaml_rule_id", "")) or f"RULE-{num:03d}"
        decision_rows = s.get("decision_rows") or s.get("rows") or []

        subrules: list[dict] = []
        for ridx, row in enumerate(decision_rows):
            if not isinstance(row, dict):
                continue
            subrules.append(_row_to_subrule(rule_id, row, ridx))

        # Carry step-level conditions/actions verbatim when the enricher
        # provides them (improves leaf-rule fidelity); otherwise fall back to
        # the terminal action so the leaf synthesis still has an action blob.
        step_conditions = [_txt(c) for c in (s.get("conditions") or []) if _txt(c)]
        step_actions = [_txt(a) for a in (s.get("actions") or []) if _txt(a)]
        if not step_actions and _txt(s.get("terminal_action", "")):
            step_actions = [_txt(s.get("terminal_action", ""))]

        rules.append({
            "rule_id": rule_id,
            "step_number": num,
            "description": _txt(s.get("question", s.get("title", ""))),
            "section": _txt(s.get("section", "")),
            "conditions": step_conditions,
            "actions": step_actions,
            "output": _txt(s.get("output_text", "")),
            "references": [],
            "urls": [],
            "tooling_allowed": True,
            "is_out_of_scope": bool(s.get("is_out_of_scope")),
            "aggregation_rule": None,
            "navigation": None,
            "subrules": subrules,
        })

    return {"metadata": metadata, "rules": rules}


# ── LLM routing enricher ──────────────────────────────────────────────────────
def _routing_schema_hint() -> str:
    return json.dumps({
        "patches": [{
            "step_number": "int — the step this patch applies to",
            "aggregation_rule": "one of: applicable_only | any | xor_one | first_match | always_met | null",
            "subrules": [{
                "subrule_id": "str — must match a draft subrule_id",
                "applicable_when": "str — guard like 'Provider is individual', else ''",
                "navigation": {"op": "goto|next|stop", "step_number": "int|null"},
                "is_out_of_scope": "bool",
            }],
        }],
    }, indent=2)


def _enrich_routing_with_llm(state: "PipelineState", cfg: "PipelineConfig",
                             draft: dict) -> dict:
    """Ask the LLM to infer ONLY routing fields the flat writer drops.

    Returns a {step_number: patch} map. Empty when the LLM is unavailable —
    the deterministic draft (plus persist_ir's own heuristics) still stands."""
    rules = draft.get("rules") or []
    if not rules:
        return {}

    # Compact per-step view: verbatim text the LLM reasons over.
    brief = []
    for r in rules[:40]:
        brief.append({
            "step_number": r["step_number"],
            "rule_id": r["rule_id"],
            "question": r["description"][:400],
            "output": r["output"][:600],
            "subrules": [{
                "subrule_id": sr["subrule_id"],
                "if": sr["description"][:200],
                "then": " ".join(sr["actions"])[:300],
                "out": sr["output"][:300],
            } for sr in r["subrules"][:12]],
        })

    declared = sorted(r["step_number"] for r in rules)
    prompt = f"""You are a claims-audit routing reasoner. Below is a draft SOP
broken into steps and decision sub-rules. The verbatim text is correct — DO NOT
rewrite it. Your ONLY job is to infer the routing metadata that drives the
execution engine's step cursor:

  • aggregation_rule: set "applicable_only" when a step's sub-rules are
    mutually-exclusive branches gated by a condition (e.g. individual-vs-group),
    so only the applicable branch is evaluated. Otherwise null.
  • applicable_when: for each such branch sub-rule, the guard phrase (verbatim
    from its text), e.g. "Provider is individual". Else "".
  • navigation: when a sub-rule's text says "skip/proceed/go to step N", set
    {{"op":"goto","step_number":N}}. Only use step numbers in {declared}.
  • is_out_of_scope: true only when the text says the line/claim is out of
    scope or to stop further auditing.

Return STRICT JSON matching this contract (patches only — omit steps with no
routing to add):
{_routing_schema_hint()}

Draft steps:
{json.dumps(brief, indent=2)[:16000]}
"""

    result = _llm_call(cfg, prompt, fallback={"patches": []},
                       agent_name="ir_maker_routing",
                       provider="anthropic",
                       expected_type=dict,
                       required_keys=["patches"],
                       stage="ir_synthesis_stage",
                       max_tokens=4096)
    patches: dict[int, dict] = {}
    for p in (result.get("patches") if isinstance(result, dict) else []) or []:
        if not isinstance(p, dict):
            continue
        sn = _int_or_none(p.get("step_number"))
        if sn is not None:
            patches[sn] = p
    return patches


_AGG_RULE_VALUES = {
    "applicable_only", "any", "any_clean", "xor_one", "exactly_one",
    "only_one", "first_match", "first", "always_met", "always",
}


def _apply_patches(draft: dict, patches: dict[int, dict], declared: set) -> int:
    """Merge LLM routing patches onto the draft IN PLACE. Returns #fields touched."""
    touched = 0
    for r in draft.get("rules") or []:
        p = patches.get(r["step_number"])
        if not p:
            continue
        agg = p.get("aggregation_rule")
        if isinstance(agg, str) and agg.strip().lower() in _AGG_RULE_VALUES:
            r["aggregation_rule"] = agg.strip().lower()
            touched += 1
        by_id = {sr["subrule_id"]: sr for sr in r["subrules"]}
        for sp in (p.get("subrules") or []):
            if not isinstance(sp, dict):
                continue
            sr = by_id.get(_txt(sp.get("subrule_id")))
            if sr is None:
                continue
            aw = _txt(sp.get("applicable_when"))
            if aw and not sr.get("applicable_when"):
                sr["applicable_when"] = aw
                touched += 1
            nav = sp.get("navigation")
            if isinstance(nav, dict) and nav.get("op") == "goto":
                tgt = _int_or_none(nav.get("step_number"))
                if tgt in declared and not sr.get("navigation"):
                    sr["navigation"] = {"op": "goto", "step_number": tgt}
                    touched += 1
            if sp.get("is_out_of_scope") is True and not sr.get("is_out_of_scope"):
                # carried as a hint; persist_ir derives OOS from text too
                sr["is_out_of_scope"] = True
                touched += 1
    return touched


# ── LLM hierarchy structurer (structure-by-reference) ────────────────────────
#
# The deterministic draft puts every decision row at depth 0 (flat siblings),
# so a nested SOP table like "A different POS → {Both Telehealth, Telehealth+
# other, Office Visit}" loses its hierarchy. This reconstructs that hierarchy
# WITHOUT inventing or dropping anything: the LLM emits ONLY a tree of existing
# subrule_ids (plus optional synthetic group headers), and the deterministic
# re-nester copies the verbatim rows by id and re-attaches any id the LLM
# omitted. Net result: the same rows, correctly nested.

def _collect_subrule_ids(subrules: list) -> list:
    ids: list[str] = []
    for sr in subrules or []:
        ids.append(sr.get("subrule_id", ""))
        ids.extend(_collect_subrule_ids(sr.get("subrules") or []))
    return ids


def _structure_schema_hint() -> str:
    return json.dumps({
        "tree": [{
            "ref": "str|null — an EXISTING subrule_id this node is; null = synthetic group header",
            "group_label": "str — REQUIRED when ref is null: the shared parent condition verbatim, e.g. 'A different POS'",
            "children": ["<recursive nodes — sub-cases nested under this node>"],
        }],
    }, indent=2)


def _restructure_step_with_llm(state, cfg, rule: dict) -> Optional[list]:
    """Return a nested id-tree for one step's subrules, or None to keep flat.

    Only fires for steps with enough rows to plausibly nest. The LLM reasons
    over verbatim text but returns STRUCTURE ONLY (subrule_id references), so it
    cannot rewrite or drop rule content."""
    flat = rule.get("subrules") or []
    if len(flat) < 4:
        return None

    brief = [{
        "subrule_id": sr.get("subrule_id", ""),
        "if": _txt(sr.get("description", ""), 240),
        "then": _txt(" ".join(sr.get("actions") or []), 240),
    } for sr in flat]

    prompt = f"""You are a claims-audit SOP structurer. Below are the decision
rows of ONE step, currently a flat list. Many SOP tables are actually nested:
several rows are sub-cases of a shared parent condition (e.g. rows "Both
Telehealth", "Telehealth + other POS", "Office Visit" are all sub-cases of a
parent "A different POS"; "Affiliated" / "Non-affiliated" are sub-cases of
"duplicate except for the provider").

Reconstruct the natural hierarchy. RULES:
• Reference rows ONLY by their subrule_id. NEVER rewrite, merge, or invent rule text.
• EVERY subrule_id below MUST appear EXACTLY ONCE somewhere in the tree.
• When rows share a parent condition that is NOT itself one of the rows, create a
  synthetic group node: ref=null, group_label=<the shared condition, verbatim
  from the rows' text>, and nest the member rows as its children.
• A row that is already a standalone top-level case stays at the top level
  (ref=<its id>, children=[]).
• Nest as deep as the logic requires (sub-sub-cases are allowed).

Return STRICT JSON matching this contract:
{_structure_schema_hint()}

Step {rule.get('rule_id')} rows:
{json.dumps(brief, indent=2)[:12000]}
"""
    result = _llm_call(cfg, prompt, fallback={"tree": []},
                       agent_name="ir_structurer",
                       provider="anthropic",
                       expected_type=dict,
                       required_keys=["tree"],
                       stage="ir_synthesis_stage",
                       max_tokens=4096)
    tree = result.get("tree") if isinstance(result, dict) else None
    return tree or None


def _apply_structure(rule: dict, tree: list) -> tuple[int, int]:
    """Rebuild ``rule['subrules']`` as a nested tree by reference.

    Guarantees completeness: every original subrule (at ANY depth) appears
    exactly once. Any id the LLM omitted (or referenced more than once /
    unknown) is re-attached so NOTHING is ever dropped. Verbatim content is
    copied from the original rows by id — the tree only supplies structure.

    Non-destructive: when the tree references a row that ALREADY has nested
    children (the new PDF/HTML synthesis emits pre-nested rows), those children
    are preserved and merged with any the tree adds, rather than overwritten.
    Returns (original_subrule_count, placed_count)."""
    flat = rule.get("subrules") or []

    # Index EVERY original id (recursively) so refs resolve at any depth and the
    # completeness pass below can recover a dropped grandchild, not just a row.
    def _index(subs: list, acc: dict) -> dict:
        for sr in subs or []:
            if isinstance(sr, dict):
                acc[sr.get("subrule_id", "")] = sr
                _index(sr.get("subrules") or [], acc)
        return acc

    by_id = _index(flat, {})
    used: set[str] = set()
    gcount = [0]

    def _mark_all(sr: dict) -> None:
        for cid in _collect_subrule_ids([sr]):
            used.add(cid)

    def build(node):
        if not isinstance(node, dict):
            return None
        ref = _txt(node.get("ref")) if node.get("ref") is not None else ""
        children = node.get("children") or []
        if ref and ref in by_id and ref not in used:
            used.add(ref)
            base = by_id[ref]
            original_children = base.get("subrules") or []
            built = [b for b in (build(c) for c in children) if b]
            built_ids = set(_collect_subrule_ids(built))
            # Preserve pre-existing children the tree didn't re-place, so an
            # already-nested row never loses its descendants.
            for oc in original_children:
                if not isinstance(oc, dict):
                    continue
                ocid = oc.get("subrule_id", "")
                if ocid not in used and ocid not in built_ids:
                    built.append(oc)
                    _mark_all(oc)
            base["subrules"] = built
            return base
        if not ref:  # synthetic group header
            kids = [b for b in (build(c) for c in children) if b]
            if not kids:
                return None
            gcount[0] += 1
            return {
                "subrule_id": f"{rule.get('rule_id', 'RULE')}-G{gcount[0]:02d}",
                "table_name": "",
                "description": _txt(node.get("group_label", "")),
                "conditions": [],
                "actions": [],
                "output": "",
                "applicable_when": "",
                "navigation": None,
                "subrules": kids,
            }
        return None  # unknown id or already used — handled by completeness pass

    new_subrules = [b for b in (build(n) for n in (tree or [])) if b]

    # Completeness: re-attach any original top-level row the tree did not place,
    # keeping its existing subtree intact. _mark_all then claims its descendants
    # so a preserved grandchild is never re-attached a second time.
    for sr in flat:
        sid = sr.get("subrule_id", "")
        if sid not in used:
            sr["subrules"] = sr.get("subrules") or []
            new_subrules.append(sr)
            _mark_all(sr)

    rule["subrules"] = new_subrules
    return len(by_id), len(used)


def _restructure_draft(state, cfg, draft: dict) -> int:
    """Nest each step's flat subrules via the LLM structurer. Returns #steps
    restructured. Completeness is enforced per step; a step is left flat on any
    error or if the LLM adds nothing."""
    restructured = 0
    for r in draft.get("rules") or []:
        flat = r.get("subrules") or []
        if len(flat) < 4:
            continue
        # The new PDF/HTML synthesis already emits correctly-nested decision
        # rows. The LLM structurer is a FLAT→nested reconstructor that only sees
        # top-level rows, so running it on already-nested input would re-nest by
        # top-level id and discard the existing hierarchy. Leave it untouched.
        if any(isinstance(sr, dict) and sr.get("subrules") for sr in flat):
            continue
        original_ids = set(_collect_subrule_ids(flat))
        try:
            tree = _restructure_step_with_llm(state, cfg, r)
            if not tree:
                continue
            _apply_structure(r, tree)
            new_ids = set(_collect_subrule_ids(r.get("subrules") or []))
            # Hard completeness invariant (the re-nester's completeness pass
            # guarantees this; we assert + flag rather than ever ship a drop).
            if not original_ids.issubset(new_ids):
                log.error("ir_structurer: %s would drop ids %s — flagged",
                          r.get("rule_id"), original_ids - new_ids)
            # Count only when depth actually increased.
            if any(sr.get("subrules") for sr in r.get("subrules") or []):
                restructured += 1
        except Exception as exc:
            log.warning("ir_structurer step %s failed (%s) — flat", r.get("rule_id"), exc)
    return restructured


# ── Agent 1 — ir_maker ────────────────────────────────────────────────────────
def ir_maker(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    steps = state.get("enriched_steps") or state.get("steps") or []
    if not steps:
        return {}

    draft = build_draft_ir(state)
    source = "deterministic_draft"
    declared = {r["step_number"] for r in draft.get("rules") or []}

    # Completeness baseline: the full set of leaf rows in the flat draft.
    leaf_ids_before = {sid for r in draft.get("rules") or []
                       for sid in _collect_subrule_ids(r.get("subrules") or [])}

    # 1) Infer routing metadata FIRST, while every row is a flat top-level
    #    sibling (so the router sees all of them and _apply_patches' by_id
    #    resolves). These fields are set on the row dicts in place, so they
    #    survive the restructuring below (which moves the SAME dicts).
    try:
        patches = _enrich_routing_with_llm(state, cfg, draft)
        if patches:
            n = _apply_patches(draft, patches, declared)
            if n:
                source = "llm_routing_enriched"
                log.info("ir_maker: applied %d routing fields across %d step patches",
                         n, len(patches))
    except Exception as exc:
        log.warning("ir_maker: LLM routing enrichment failed (%s) — draft stands", exc)

    # 2) Reconstruct nested hierarchy (rules → subrules → sub-subrules) by
    #    reference, so nested SOP tables (e.g. "A different POS → {Telehealth,
    #    Telehealth+other, Office Visit}") don't collapse to flat siblings.
    try:
        n_struct = _restructure_draft(state, cfg, draft)
        if n_struct:
            source = "llm_hierarchy_structured"
            log.info("ir_maker: restructured %d steps into nested subrules", n_struct)
    except Exception as exc:
        log.warning("ir_maker: hierarchy structuring failed (%s) — flat draft stands", exc)

    # Completeness assertion: nothing the draft captured may be lost.
    leaf_ids_after = {sid for r in draft.get("rules") or []
                      for sid in _collect_subrule_ids(r.get("subrules") or [])}
    missing = leaf_ids_before - leaf_ids_after
    if missing:
        log.error("ir_maker: restructuring dropped %d rows %s", len(missing), sorted(missing))

    if job_id:
        ctx_write(cfg, job_id, "draft", draft)
        ctx_audit_trail(cfg, job_id, "ir_maker", "OK",
                        f"{len(draft.get('rules', []))} rules, source={source}, "
                        f"leaves={len(leaf_ids_after)}")
    return {"sop_ir": draft, "sop_ir_source": source}


# ── Agent 2 — ir_checker ──────────────────────────────────────────────────────
def _dedupe_step_numbers(draft: dict) -> bool:
    """Repair colliding explicit step_numbers by re-sequencing in source order."""
    seen: set[int] = set()
    changed = False
    nxt = 1
    used = {r.get("step_number") for r in draft.get("rules") or []}
    for r in draft.get("rules") or []:
        sn = r.get("step_number")
        if sn in seen:
            while nxt in used:
                nxt += 1
            r["step_number"] = nxt
            used.add(nxt)
            changed = True
        seen.add(r.get("step_number"))
    return changed


def _strip_unresolved_navigation(draft: dict) -> bool:
    declared = {r.get("step_number") for r in draft.get("rules") or []}
    changed = False

    def _walk(sr: dict):
        nonlocal changed
        nav = sr.get("navigation")
        if isinstance(nav, dict) and nav.get("op") == "goto":
            if nav.get("step_number") not in declared:
                sr["navigation"] = None
                changed = True
        for c in sr.get("subrules") or []:
            _walk(c)

    for r in draft.get("rules") or []:
        nav = r.get("navigation")
        if isinstance(nav, dict) and nav.get("op") == "goto" and nav.get("step_number") not in declared:
            r["navigation"] = None
            changed = True
        for sr in r.get("subrules") or []:
            _walk(sr)
    return changed


def ir_checker(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    job_id = state.get("job_id", "")
    draft = state.get("sop_ir")
    if not draft:
        return {}

    SopIR, validate_ir = _load_sop_ir()
    if SopIR is None:
        # No schema available (standalone CLI) — keep the draft, flag unchecked.
        result = {"sop_ir_validation": {"ok": None, "errors": ["validator unavailable"]}}
        if job_id:
            ctx_audit_trail(cfg, job_id, "ir_checker", "SKIP", "validator unavailable")
        return result

    errors: list[str] = []
    ok = False
    for attempt in range(3):
        try:
            ir = SopIR.model_validate(draft)
        except Exception as exc:
            errors = [f"ERROR: IR failed schema parse: {exc}"]
            # Deterministic repairs that don't need the typed model.
            if not (_dedupe_step_numbers(draft) or _strip_unresolved_navigation(draft)):
                break
            continue
        ok, errors = validate_ir(ir)
        if ok:
            # normalise the draft back from the validated model so downstream
            # persists exactly what passed validation.
            draft = ir.model_dump(mode="json")
            break
        # bounded repair: drop dangling navigation + dedupe step numbers
        repaired = _strip_unresolved_navigation(draft) or _dedupe_step_numbers(draft)
        if not repaired:
            break

    validation = {"ok": ok, "errors": errors}
    if job_id:
        ctx_write(cfg, job_id, "validation", validation)
        ctx_audit_trail(cfg, job_id, "ir_checker", "OK" if ok else "FLAGGED",
                        f"{len([e for e in errors if e.startswith('ERROR:')])} errors")
    if not ok and errors:
        log.warning("ir_checker: IR has %d residual routing issues: %s",
                    len([e for e in errors if e.startswith('ERROR:')]), errors[:3])

    # Accumulate this document's IR so pipeline_runner can persist EVERY SOP in
    # a multi-doc crawl. content_hash matches AuditSop(job, content_hash).
    entry = {
        "content_hash": state.get("content_hash", ""),
        "url": state.get("current_url", ""),
        "ir": draft,
        "source": state.get("sop_ir_source", "deterministic_draft"),
        "validation": validation,
    }
    return {
        "sop_ir": draft,
        "sop_ir_validation": validation,
        "sop_ir_documents": [entry],
    }
