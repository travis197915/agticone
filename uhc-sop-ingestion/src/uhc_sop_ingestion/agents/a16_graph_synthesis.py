"""
a16_graph_synthesis.py — Agentic Knowledge-Graph Construction

A multi-agent flow that reasons over the parsed SOP state like a senior
claims auditor, building the canonical SOP knowledge graph entirely via
LLM reasoning instead of brittle field-name mapping.

Architecture
────────────
Redis acts as the shared blackboard / context memory.  Every agent reads
the prior agents' outputs from Redis, performs its specialised reasoning
via an LLM call, and writes its result back to Redis under a structured
namespace:

    sop:graph:{job_id}:{section}        — JSON payload per section
    sop:graph:{job_id}:meta             — synthesis metadata + audit trail

Agent pipeline (executed sequentially in graph_synthesis_stage):

  1. agent_document_profiler        GPT-4o    → DOCUMENT + META nodes
  2. agent_pre_section_synthesizer  Claude    → PRE_SECTION + PRE_RULE nodes
  3. agent_step_decomposer          GPT-4o    → STEP nodes
  4. agent_decision_classifier      Claude    → DECISION nodes + GOTO edges
  5. agent_code_grounder            GPT-4o    → CODE nodes + USES_CODE edges
  6. agent_reference_resolver       GPT-4o    → REFERENCE + GROUP_LIMIT
                                                + DATE_COND + ANNOTATION nodes
  7. agent_semantic_edge_reasoner   Claude    → OVERRIDES, IMPLIES, GUARDS,
                                                CITED_BY, APPLIES_TO edges
  8. agent_graph_assembler          (pure)    → validate, dedupe, attach to state

Guard rails (the whole point of the agentic flow):
  • Strict JSON schema for every LLM response
  • Coverage validators: every parsed step → STEP node; every parsed code
    referenced → CODE node; doc → DOCUMENT node
  • Edge-integrity validators: every edge source/target must resolve to a
    declared node key
  • Retry-with-critique on any validator failure (max 3 per agent)
  • Deterministic fallback: if all LLM attempts fail, fall back to
    `uhc_sop_ingestion.graph_builder.build_audit_graph` so PG/Neo4j are
    NEVER left empty
"""
from __future__ import annotations

import json
import logging
import re as _re
from typing import TYPE_CHECKING, Any

from .a07_enrich import _llm_call

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Canonical schema (must match graph_builder.py + AuditGraphNode/Edge models)
# ──────────────────────────────────────────────────────────────────────────────

NODE_TYPES = {
    "DOCUMENT", "META", "PRE_SECTION", "PRE_RULE",
    "STEP", "DECISION", "ANNOTATION",
    "GROUP_LIMIT", "CODE", "DATE_COND", "REFERENCE",
}

EDGE_TYPES = {
    # Structural
    "HAS_META", "HAS_PRE_SECTION", "HAS_RULE",
    "HAS_STEP", "HAS_DECISION", "HAS_ANNOTATION",
    "HAS_GROUP_LIMIT", "HAS_CODE_REF", "HAS_DATE_COND",
    "REFERENCES", "GOTO",
    # Semantic (LLM-inferred)
    "USES_CODE", "OVERRIDES", "IMPLIES", "CITED_BY", "GUARDS", "APPLIES_TO",
}

DECISION_TYPES = {
    "DENY", "ALLOW", "BYPASS", "PEND", "REFER",
    "SYSTEM", "STOP", "WAIVE", "CONDITIONAL",
    "OVERRIDE", "ELIGIBILITY", "NOTE",
}


# ──────────────────────────────────────────────────────────────────────────────
# Redis shared-context blackboard
# ──────────────────────────────────────────────────────────────────────────────

_CONTEXT_TTL_SECONDS = 24 * 3600  # 1 day


def _redis(cfg: "PipelineConfig"):
    from ..config import get_redis
    return get_redis(cfg)


def _ctx_key(job_id: str, section: str) -> str:
    return f"sop:graph:{job_id}:{section}"


def ctx_write(cfg, job_id: str, section: str, payload: Any) -> None:
    """Persist an agent's output to the shared Redis context."""
    try:
        r = _redis(cfg)
        r.set(_ctx_key(job_id, section), json.dumps(payload, default=str))
        r.expire(_ctx_key(job_id, section), _CONTEXT_TTL_SECONDS)
    except Exception as exc:
        log.warning("ctx_write[%s] failed: %s", section, exc)


def ctx_read(cfg, job_id: str, section: str, default=None):
    """Read another agent's output from the shared Redis context."""
    try:
        r = _redis(cfg)
        raw = r.get(_ctx_key(job_id, section))
        return json.loads(raw) if raw else default
    except Exception as exc:
        log.warning("ctx_read[%s] failed: %s", section, exc)
        return default


def ctx_audit_trail(cfg, job_id: str, agent: str, status: str,
                    detail: str = "") -> None:
    """Append an audit-trail entry so we can see which agent did what."""
    try:
        r = _redis(cfg)
        key = _ctx_key(job_id, "audit_trail")
        entry = json.dumps({"agent": agent, "status": status, "detail": detail[:300]})
        r.rpush(key, entry)
        r.expire(key, _CONTEXT_TTL_SECONDS)
    except Exception:
        pass


def ctx_wipe(cfg, job_id: str) -> None:
    """Clear all context keys for this job (called at the start of synthesis)."""
    try:
        r = _redis(cfg)
        for k in r.scan_iter(f"sop:graph:{job_id}:*"):
            r.delete(k)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Generic graph helpers (used by all agents)
# ──────────────────────────────────────────────────────────────────────────────

def _make_node(key: str, ntype: str, label: str, **details) -> dict:
    return {
        "key": key, "type": ntype, "label": (label or "")[:255],
        "details": {k: v for k, v in details.items() if v is not None},
        "ref_table": "", "ref_id": None, "order": int(details.get("order", 0) or 0),
    }


def _make_edge(src: str, tgt: str, rel: str, label: str = "", **details) -> dict:
    return {
        "source": src, "target": tgt, "rel": rel, "label": (label or "")[:255],
        "details": details,
    }


def _strip_nul(obj: Any) -> Any:
    """Recursively strip NUL (\\x00) bytes — PostgreSQL rejects them in text/jsonb."""
    if isinstance(obj, str):
        return obj.replace("\x00", "") if "\x00" in obj else obj
    if isinstance(obj, dict):
        return {k: _strip_nul(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_nul(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_strip_nul(x) for x in obj)
    return obj


def _hashable_str(v: Any) -> str | None:
    """Coerce a value to a non-empty NUL-free str, or return None if it cannot be."""
    if isinstance(v, str):
        s = v.replace("\x00", "").strip() if "\x00" in v else v.strip()
        return s or None
    if isinstance(v, (int, float, bool)):
        return str(v)
    return None  # dict/list/None → invalid


# Tolerant alias maps — different LLMs (and different prompts) emit fields
# under slightly different names. Be generous with the schema we accept.
_NODE_KEY_ALIASES   = ("key", "id", "node_key", "name")
_NODE_TYPE_ALIASES  = ("type", "node_type", "nodeType", "kind", "category")
_NODE_LABEL_ALIASES = ("label", "title", "display", "display_name", "name")
_NODE_DETAIL_ALIASES = ("details", "properties", "props", "data", "attributes",
                        "DOCUMENT.details", "META.details", "PRE_SECTION.details",
                        "PRE_RULE.details", "STEP.details", "DECISION.details",
                        "ANNOTATION.details", "GROUP_LIMIT.details",
                        "CODE.details", "DATE_COND.details", "REFERENCE.details")

_EDGE_SRC_ALIASES  = ("source", "from", "src", "start", "source_key", "start_node")
_EDGE_TGT_ALIASES  = ("target", "to", "dst", "end", "target_key", "end_node")
_EDGE_REL_ALIASES  = ("rel", "rel_type", "relationship", "type", "relation",
                      "rel_name", "relType", "label")
_EDGE_DETAIL_ALIASES = ("details", "properties", "props", "data", "attributes")

# Common alias forms the LLM might emit for the canonical UPPER_SNAKE types.
_TYPE_ALIAS_MAP = {
    "DECISION": "DECISION", "decision": "DECISION",
    "PRESECTION": "PRE_SECTION", "PRE-SECTION": "PRE_SECTION",
    "PRESEC": "PRE_SECTION", "SECTION": "PRE_SECTION",
    "PRERULE": "PRE_RULE", "PRE-RULE": "PRE_RULE",
    "RULE": "PRE_RULE",
    "GROUPLIMIT": "GROUP_LIMIT", "GROUP-LIMIT": "GROUP_LIMIT",
    "DATECOND": "DATE_COND", "DATECONDITION": "DATE_COND",
    "DATE-CONDITION": "DATE_COND", "DATE_CONDITION": "DATE_COND",
    "REF": "REFERENCE", "DOC": "DOCUMENT", "SOPDOCUMENT": "DOCUMENT",
    "METADATA": "META",
}

_REL_ALIAS_MAP = {
    "HASMETA": "HAS_META", "HAS-META": "HAS_META",
    "HASPRESECTION": "HAS_PRE_SECTION", "HAS-PRE-SECTION": "HAS_PRE_SECTION",
    "HASRULE": "HAS_RULE", "HAS-RULE": "HAS_RULE",
    "HASSTEP": "HAS_STEP", "HAS-STEP": "HAS_STEP",
    "HASDECISION": "HAS_DECISION", "HAS-DECISION": "HAS_DECISION",
    "HASANNOTATION": "HAS_ANNOTATION", "HAS-ANNOTATION": "HAS_ANNOTATION",
    "HASGROUPLIMIT": "HAS_GROUP_LIMIT", "HAS-GROUP-LIMIT": "HAS_GROUP_LIMIT",
    "HASCODEREF": "HAS_CODE_REF", "HAS-CODE-REF": "HAS_CODE_REF",
    "HASCODE": "HAS_CODE_REF",
    "HASDATECOND": "HAS_DATE_COND", "HAS-DATE-COND": "HAS_DATE_COND",
    "USESCODE": "USES_CODE", "USES-CODE": "USES_CODE",
    "CITEDBY": "CITED_BY", "CITED-BY": "CITED_BY",
    "APPLIESTO": "APPLIES_TO", "APPLIES-TO": "APPLIES_TO",
    "GOTO": "GOTO", "GO-TO": "GOTO", "GO_TO": "GOTO",
}


def _first_present(d: dict, aliases: tuple) -> Any:
    """Return the value for the first alias key present in d (else None)."""
    for k in aliases:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _normalize_type(raw: Any) -> str | None:
    """Map an LLM-emitted type string to a canonical NODE_TYPE."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip().upper().replace(" ", "_").replace("-", "_")
    if s in NODE_TYPES:
        return s
    if s in _TYPE_ALIAS_MAP:
        return _TYPE_ALIAS_MAP[s]
    flat = s.replace("_", "")
    return _TYPE_ALIAS_MAP.get(flat)


# Key-prefix → canonical NODE_TYPE inference. Used when the LLM omits an
# explicit type but follows the canonical key naming from the prompts.
_KEY_PREFIX_PATTERNS: list[tuple[str, str]] = [
    (r"^doc$",                         "DOCUMENT"),
    (r"^meta$",                        "META"),
    (r"^pre_\d+_r\d+$",                "PRE_RULE"),
    (r"^pre_\d+$",                     "PRE_SECTION"),
    (r"^step_\d+_d\d+[a-z]?$",         "DECISION"),
    (r"^step_\d+$",                    "STEP"),
    (r"^(ann_|annotation_|note_)",     "ANNOTATION"),
    (r"^(grp_|group_|grouplimit_)\d+", "GROUP_LIMIT"),
    (r"^code_",                        "CODE"),
    (r"^coderef_",                     "REFERENCE"),
    (r"^(date_|datecond_)\d+",         "DATE_COND"),
    (r"^(ref_|reference_)\d+",         "REFERENCE"),
]


def _infer_type_from_key(key: str) -> str | None:
    if not isinstance(key, str):
        return None
    for pat, t in _KEY_PREFIX_PATTERNS:
        if _re.match(pat, key):
            return t
    return None


def _normalize_rel(raw: Any) -> str | None:
    """Map an LLM-emitted relationship string to a canonical EDGE_TYPE."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip().upper().replace(" ", "_").replace("-", "_")
    if s in EDGE_TYPES:
        return s
    if s in _REL_ALIAS_MAP:
        return _REL_ALIAS_MAP[s]
    flat = s.replace("_", "")
    if flat in _REL_ALIAS_MAP:
        return _REL_ALIAS_MAP[flat]
    return None


def sanitize_graph(nodes: list, edges: list) -> tuple[list[dict], list[dict]]:
    """Drop malformed LLM output BEFORE it can break set/dict operations.

    Generous about field naming — accepts common aliases produced by GPT-4o
    and Claude (e.g. node_type/type, from/source, rel_type/rel, properties/
    details, CODE.details, etc.). Strict about types: every output node has
    a hashable string key + a canonical NODE_TYPE; every output edge has
    string src/tgt + a canonical EDGE_TYPE.
    """
    clean_nodes: list[dict] = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        key = _hashable_str(_first_present(n, _NODE_KEY_ALIASES))
        if key is None:
            continue
        ntype = (_normalize_type(_first_present(n, _NODE_TYPE_ALIASES))
                 or _infer_type_from_key(key))
        if ntype is None:
            continue
        details = _first_present(n, _NODE_DETAIL_ALIASES) or {}
        if not isinstance(details, dict):
            details = {"_raw": str(details)[:240]}
        label = _first_present(n, _NODE_LABEL_ALIASES) or ""
        clean_nodes.append({
            "key": key,
            "type": ntype,
            "label": _strip_nul((str(label) or ""))[:255],
            "details": _strip_nul(details),
            "ref_table": _strip_nul(str(n.get("ref_table", "")))[:64],
            "ref_id": n.get("ref_id"),
            "order": int(n.get("order", 0) or 0),
        })

    clean_edges: list[dict] = []
    for e in edges or []:
        if not isinstance(e, dict):
            continue
        src = _hashable_str(_first_present(e, _EDGE_SRC_ALIASES))
        tgt = _hashable_str(_first_present(e, _EDGE_TGT_ALIASES))
        rel = _normalize_rel(_first_present(e, _EDGE_REL_ALIASES))
        if src is None or tgt is None or rel is None:
            continue
        details = _first_present(e, _EDGE_DETAIL_ALIASES) or {}
        if not isinstance(details, dict):
            details = {"_raw": str(details)[:240]}
        clean_edges.append({
            "source": src, "target": tgt, "rel": rel,
            "label": _strip_nul((str(e.get("label", "")) or ""))[:255],
            "details": _strip_nul(details),
        })
    return clean_nodes, clean_edges


def _validate_graph(nodes: list[dict], edges: list[dict]) -> dict:
    """Validate the canonical graph.

    Returns a dict with three buckets:
      hard:  structural errors that justify a full deterministic-fallback
             (no DOCUMENT, duplicate node keys, no nodes at all)
      soft:  per-edge errors that should just drop the offending edge
      details: per-edge details for the soft errors (with edge index)
    """
    hard: list[str] = []
    soft: list[str] = []
    bad_edge_idx: set[int] = set()

    keys: set[str] = set()
    if not nodes:
        hard.append("no nodes")
    for n in nodes:
        if n["key"] in keys:
            hard.append(f"duplicate node key {n['key']}")
        keys.add(n["key"])
    if nodes and not any(n.get("type") == "DOCUMENT" for n in nodes):
        hard.append("no DOCUMENT node found")
    for i, e in enumerate(edges):
        if e["source"] not in keys:
            soft.append(f"edge source missing: {e['source']}")
            bad_edge_idx.add(i)
        if e["target"] not in keys:
            soft.append(f"edge target missing: {e['target']}")
            bad_edge_idx.add(i)
    return {"hard": hard, "soft": soft, "bad_edge_idx": bad_edge_idx}


def _compact_state_for_llm(state: "PipelineState") -> dict:
    """Compact subset of state to feed into LLMs without blowing context."""
    meta = state.get("metadata") or {}
    steps = state.get("enriched_steps") or state.get("steps") or []
    return {
        "title":     meta.get("title", ""),
        "platform":  meta.get("platform", ""),
        "effective_date": str(meta.get("effective_date", "")),
        "revision_date":  str(meta.get("revision_date", "")),
        "summary":   (state.get("llm_summary", "") or "")[:600],
        "num_pre_sections": len(state.get("pre_sections") or []),
        "num_steps":  len(steps),
        "num_codes":  len(state.get("detected_codes") or []),
        "num_links":  len(state.get("links") or []),
        "num_group_rules": len(state.get("group_rules") or []),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Agent 1 — Document Profiler  (GPT-4o)
# ──────────────────────────────────────────────────────────────────────────────

def agent_document_profiler(state: "PipelineState",
                            cfg: "PipelineConfig") -> dict:
    """Emit the DOCUMENT (GOD) node + a METADATA node."""
    job_id = state.get("job_id", "")
    if not job_id:
        return {}
    ctx_wipe(cfg, job_id)

    summary = _compact_state_for_llm(state)
    prompt = f"""You are a senior claims-audit knowledge graph architect.

Produce exactly TWO nodes for this SOP — the root DOCUMENT and a METADATA child.

Return STRICT JSON:
{{
  "nodes": [
    {{"key":"doc","type":"DOCUMENT","label":"<sop title>",
      "details":{{"platform":"...","effective_date":"...","revision_date":"...",
                  "summary":"<audit-focused one-sentence purpose>"}}}},
    {{"key":"meta","type":"META","label":"Document Metadata",
      "details":{{"platform":"...","effective_date":"...","revision_date":"...",
                  "lob":[...], "audience":[...], "state_div":"..."}}}}
  ],
  "edges": [
    {{"source":"doc","target":"meta","rel":"HAS_META","label":""}}
  ]
}}

Input SOP summary:
{json.dumps(summary, indent=2)}

Full metadata:
{json.dumps(state.get("metadata") or {}, default=str, indent=2)[:2000]}
"""

    result = _llm_call(cfg, prompt, fallback={"nodes": [], "edges": []},
                       agent_name="agent_document_profiler",
                       provider="openai",
                       expected_type=dict,
                       required_keys=["nodes", "edges"],
                       stage="graph_synthesis_stage",
                       max_tokens=4096)
    if not isinstance(result, dict) or not result.get("nodes"):
        ctx_audit_trail(cfg, job_id, "agent_document_profiler", "FALLBACK")
        return {}
    ctx_write(cfg, job_id, "document", result)
    ctx_audit_trail(cfg, job_id, "agent_document_profiler", "OK",
                    f"{len(result.get('nodes', []))} nodes")
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Agent 2 — Pre-Section Synthesizer  (Claude — reasoning task)
# ──────────────────────────────────────────────────────────────────────────────

def agent_pre_section_synthesizer(state: "PipelineState",
                                  cfg: "PipelineConfig") -> dict:
    """Emit PRE_SECTION nodes + PRE_RULE child nodes for every distinct rule."""
    job_id = state.get("job_id", "")
    pre = state.get("pre_sections") or []
    if not pre:
        return {}

    sections = []
    for i, ps in enumerate(pre[:15]):
        text = " ".join(
            (it.get("text", str(it)) if isinstance(it, dict) else str(it))
            for it in ps.get("items", [])
        )[:900]
        sections.append({
            "idx":      i + 1,
            "name":     ps.get("name", "") or f"Pre-Section {i+1}",
            "section_id": ps.get("section_id", ""),
            "text":     text,
        })

    # Batch sections so each call's nodes+edges output stays well under the
    # token cap. A single 15-section call truncates mid-array on rule-dense
    # SOPs (the "Schema mismatch: expected dict with keys ['nodes','edges']"
    # warning); small batches keep every PRE_RULE.
    _PS_BATCH = 4
    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    for bstart in range(0, len(sections), _PS_BATCH):
        chunk = sections[bstart:bstart + _PS_BATCH]
        result = _synthesize_pre_section_batch(cfg, chunk, bstart // _PS_BATCH + 1)
        if isinstance(result, dict):
            all_nodes.extend(result.get("nodes", []) or [])
            all_edges.extend(result.get("edges", []) or [])

    payload = {"nodes": all_nodes, "edges": all_edges}
    if all_nodes:
        ctx_write(cfg, job_id, "pre_sections", payload)
        ctx_audit_trail(cfg, job_id, "agent_pre_section_synthesizer", "OK",
                        f"{len(all_nodes)} nodes, {len(all_edges)} edges "
                        f"across {(len(sections) + _PS_BATCH - 1) // _PS_BATCH} "
                        f"batch(es)")
    else:
        ctx_audit_trail(cfg, job_id, "agent_pre_section_synthesizer", "FALLBACK")
    return {}


def _synthesize_pre_section_batch(cfg: "PipelineConfig", chunk: list[dict],
                                  batch_no: int) -> dict:
    """LLM call for one batch of pre-sections → {"nodes":[...], "edges":[...]}."""
    prompt = f"""You are a senior claims auditor building the SOP knowledge graph
the way an auditor would read the document top-down.

TARGET STRUCTURE (use this as the exemplar — match its granularity exactly):

  (:SopDocument)  ← already created as "doc"
    ├── [:HAS_PRE_SECTION {{order:1}}] "Overview / DUPS Warning"
    │     └── [:HAS_RULE] "Related claims"
    │     └── [:HAS_RULE] "Similar claims"
    │     └── [:HAS_RULE] "Same claim, original denied (zero allowed)"
    ├── [:HAS_PRE_SECTION {{order:2}}] "Pre-flight Checks"
    │     ├── [:HAS_RULE] "Medicaid Reclamation → follow Medicaid Reclamation P&P FIRST"
    │     └── [:HAS_RULE] "ECT/Anesthesia → follow Facets ECT Treatment P&P FIRST"
    ├── [:HAS_PRE_SECTION {{order:4}}] "Duplicate Exceptions"
    │     ├── [:HAS_RULE] "Exception-1: Providence + type_of_bill 085|013 + REV 510|513|521|900 + POS 22|19 → Follow Warning Message Resolution, do NOT deny WCF"
    │     ├── [:HAS_RULE] "Exception-4: Monthly case services → allow multiple visits, pay once/month"
    │     └── ...

YOUR TASK:
  • Emit ONE PRE_SECTION node per input section.
       key   = "pre_<idx>"
       label = the section name (e.g. "Duplicate Exceptions")
       edge  = doc -[:HAS_PRE_SECTION {{order:<idx>}}]-> pre_<idx>
  • For EACH atomic rule, exception, exclusion, or note inside that
    section, emit a PRE_RULE child.
       key   = "pre_<idx>_r<n>"
       label = a short human-readable name like
                 "Exception-1: Providence + TOB 085|013 → Warning Message Resolution"
                 "Exclusion: Virgin Island Providers excluded from cross-billing"
                 "Note: System configured for frequency maximums"
       edge  = pre_<idx> -[:HAS_RULE]-> pre_<idx>_r<n>
  • PRE_RULE.details MUST include:
       condition     (the "if/when" part, verbatim from the SOP)
       action        (the "then/follow" part, verbatim)
       decision_type (DENY|ALLOW|BYPASS|PEND|REFER|OVERRIDE|ELIGIBILITY|NOTE)
       is_exception  (true when it overrides standard processing)
       rule_kind     ("rule" | "exception" | "exclusion" | "note")

Return STRICT JSON: {{"nodes":[...], "edges":[...]}}

Sections (batch {batch_no}):
{json.dumps(chunk, indent=2)}
"""

    result = _llm_call(cfg, prompt, fallback={"nodes": [], "edges": []},
                       agent_name=f"agent_pre_section_synthesizer_b{batch_no}",
                       provider="anthropic",
                       expected_type=dict,
                       required_keys=["nodes", "edges"],
                       stage="graph_synthesis_stage",
                       max_tokens=8192)
    return result if isinstance(result, dict) else {"nodes": [], "edges": []}


# ──────────────────────────────────────────────────────────────────────────────
# Agent 3 — Step Decomposer  (GPT-4o)
# ──────────────────────────────────────────────────────────────────────────────

def agent_step_decomposer(state: "PipelineState",
                          cfg: "PipelineConfig") -> dict:
    """Emit STEP nodes + step-level ANNOTATION children + branch annotations."""
    job_id = state.get("job_id", "")
    steps = state.get("enriched_steps") or state.get("steps") or []
    if not steps:
        return {}

    step_brief = []
    for s in steps[:30]:
        try:
            num = int(s.get("number", s.get("step_number", -1)))
        except (TypeError, ValueError):
            continue
        if num < 0:
            continue
        anns = []
        for a in (s.get("annotations") or [])[:8]:
            if isinstance(a, dict):
                anns.append({
                    "type": a.get("annotation_type") or a.get("type") or "NOTE",
                    "text": (a.get("text") or "")[:250],
                })
        step_brief.append({
            "number":      num,
            "question":    (s.get("question") or s.get("title") or "")[:300],
            "intro":       (s.get("intro_text") or "")[:300],
            "is_terminal": bool(s.get("is_terminal")),
            "terminal_action": (s.get("terminal_action") or "")[:200],
            "sub_proc":    s.get("sub_procedure_name", ""),
            "branch_yes":  (s.get("branch_yes") or "")[:250],
            "branch_no":   (s.get("branch_no") or "")[:250],
            "annotations": anns,
            "num_decisions": len(s.get("decision_rows") or s.get("rows") or []),
        })

    prompt = f"""You are a senior claims auditor. Build the STEP subgraph the
way an auditor reads the SOP top-down.

TARGET STRUCTURE EXAMPLE (match this granularity exactly):

  doc
    ├── [:HAS_STEP {{order:1}}] → step_1
    │     ├── question: "Was the claim submitted identified as a corrected claim or Void Claim?"
    │     ├── [:HAS_ANNOTATION] ann_step_1_a1 "Review claim image for frequency 7 or 8"
    │     ├── [:HAS_ANNOTATION] ann_step_1_a2 "Physician electronic – box 12A; Facility – last digit bill type 7 or 8"
    │     ├── [:HAS_ANNOTATION] ann_step_1_branch_yes "Corrected claim, NOT a duplicate → REF: Facets Claim Attachment Validation"
    │     └── [:HAS_ANNOTATION] ann_step_1_branch_no  "Continue to Step 2"
    ├── [:HAS_STEP {{order:9}}] → step_9
    │     └── (terminal) terminal_action: "(F3) Process the claim"

YOUR TASK:
  1. Emit ONE STEP node per input step.
       key   = "step_<number>"
       label = "Step <number>: <short question>"
       edge  = doc -[:HAS_STEP {{order:<number>}}]-> step_<n>
     STEP.details: step_number (int), question (str), intro (str),
                   is_terminal (bool), terminal_action (str),
                   sub_procedure_name (str).
  2. For EACH item in the step's `annotations` list, emit an ANNOTATION
     child node.
       key   = "ann_step_<n>_a<idx>"
       label = the annotation text (≤ 200 chars)
       edge  = step_<n> -[:HAS_ANNOTATION]-> ann_step_<n>_a<idx>
     ANNOTATION.details: annotation_type, source ("annotation")
  3. If `branch_yes` is non-empty, emit ANNOTATION key="ann_step_<n>_branch_yes",
     label = the yes/skip text, details.source = "branch_yes".
  4. If `branch_no` is non-empty, emit ANNOTATION key="ann_step_<n>_branch_no",
     label = the no/continue text, details.source = "branch_no".

Return STRICT JSON: {{"nodes":[...], "edges":[...]}}

Steps:
{json.dumps(step_brief, indent=2)}
"""

    result = _llm_call(cfg, prompt, fallback={"nodes": [], "edges": []},
                       agent_name="agent_step_decomposer",
                       provider="openai",
                       expected_type=dict,
                       required_keys=["nodes", "edges"],
                       stage="graph_synthesis_stage",
                       max_tokens=6144)
    if isinstance(result, dict):
        ctx_write(cfg, job_id, "steps", result)
        ctx_audit_trail(cfg, job_id, "agent_step_decomposer", "OK",
                        f"{len(result.get('nodes', []))} nodes")
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Agent 4 — Decision Classifier  (Claude — semantic reasoning)
# ──────────────────────────────────────────────────────────────────────────────

def agent_decision_classifier(state: "PipelineState",
                              cfg: "PipelineConfig") -> dict:
    """Emit DECISION nodes per step + [:HAS_DECISION] + [:GOTO] edges.

    Batched: 6 decision rows per LLM call. Keeps each response well under
    max_tokens so JSON never gets truncated mid-string.
    """
    job_id = state.get("job_id", "")
    steps = state.get("enriched_steps") or state.get("steps") or []
    if not steps:
        return {}

    rows_for_llm = []
    valid_step_nums: set[int] = set()
    for s in steps[:30]:
        try:
            num = int(s.get("number", s.get("step_number", -1)))
        except (TypeError, ValueError):
            continue
        if num < 0:
            continue
        valid_step_nums.add(num)
        decision_rows = s.get("decision_rows") or s.get("rows") or []
        for didx, dec in enumerate(decision_rows[:20]):
            if not isinstance(dec, dict):
                continue
            rows_for_llm.append({
                "step":     num,
                "row":      didx,
                "if":       (dec.get("condition_if")
                             or dec.get("if") or dec.get("condition", ""))[:300],
                "and":      (dec.get("condition_and", "") or "")[:150],
                "then":     (dec.get("action_text")
                             or dec.get("then") or dec.get("action", ""))[:400],
                "codes_hint": dec.get("codes") or [],
                "goto_hint":  dec.get("skip_to_step") or dec.get("goto_step"),
            })

    if not rows_for_llm:
        return {}

    BATCH = 6
    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    for bstart in range(0, len(rows_for_llm), BATCH):
        batch = rows_for_llm[bstart:bstart + BATCH]
        prompt = f"""You are a claims-audit reasoner. Build the DECISION subgraph
the way an auditor reads a Step 7-style decision matrix.

TARGET STRUCTURE EXAMPLE (match this naming + granularity exactly):

  step_7
    ├── [:HAS_DECISION {{decision_type:"DENY"}}] decision_row_1
    │     label: "IF claim/lines denying as CDD → Allow system to deny CDD; "
    │            "if system not denying → override Deny E51/F51"
    ├── [:HAS_DECISION {{decision_type:"DENY"}}] decision_row_2
    │     label: "IF duplicate except Provider + affiliated (same TIN, diff names) "
    │            "→ Deny: Line EX003+E51; Claim 346+F51"
    ├── [:HAS_DECISION {{decision_type:"BYPASS"}}] decision_row_6b
    │     label: "IF different POS: Telehealth + any other POS → allowed to "
    │            "bill both → Bypass + allow payment"
    ├── [:GOTO {{target_step:9}}] decision_row_1 -> step_9

YOUR TASK for each decision row in this batch:
  1. Emit ONE DECISION node.
       key   = "step_<step>_d<row>"   (e.g. "step_7_d2")
       label = a one-liner of the form
               "IF <condition> [AND <cond2>] → <action with codes/overrides>"
               (≤ 240 chars; include EX/EOB/denial codes if present)
       edge  = step_<step> -[:HAS_DECISION {{decision_type}}]-> decision node
  2. DECISION.details MUST include:
       row_id          (a friendly id like "decision_row_1", "decision_row_5a")
       condition_if    (verbatim "if" clause)
       condition_and   (verbatim "and" clause, "" if none)
       action_text     (verbatim action)
       decision_type   (DENY|ALLOW|BYPASS|PEND|REFER|SYSTEM|STOP|WAIVE|CONDITIONAL)
       eob_codes       (e.g. ["E51"])
       ex_codes        (e.g. ["003"])
       denial_codes    (e.g. ["346","F51"])
       goto_step       (int|null)
       is_final        (true if action ends the flow)
  3. If a next-step is inferable, emit [:GOTO {{target_step}}] from this
     decision to step_<goto>. Only emit GOTO when target_step is in
     {sorted(valid_step_nums)}.

Classify decision_type by reading the action text semantically.

Return STRICT JSON: {{"nodes":[...], "edges":[...]}}

Decision rows (batch {bstart // BATCH + 1}):
{json.dumps(batch, indent=2)}
"""
        result = _llm_call(cfg, prompt, fallback={"nodes": [], "edges": []},
                           agent_name=f"agent_decision_classifier_b{bstart // BATCH + 1}",
                           provider="anthropic",
                           expected_type=dict,
                           required_keys=["nodes", "edges"],
                           stage="graph_synthesis_stage",
                           max_tokens=4096)
        if isinstance(result, dict):
            all_nodes.extend(result.get("nodes", []))
            all_edges.extend(result.get("edges", []))

    payload = {"nodes": all_nodes, "edges": all_edges}
    ctx_write(cfg, job_id, "decisions", payload)
    ctx_audit_trail(cfg, job_id, "agent_decision_classifier", "OK",
                    f"{len(all_nodes)} decisions across "
                    f"{(len(rows_for_llm) + BATCH - 1) // BATCH} batches")
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Agent 5 — Code Grounder  (GPT-4o)
# ──────────────────────────────────────────────────────────────────────────────

def agent_code_grounder(state: "PipelineState",
                        cfg: "PipelineConfig") -> dict:
    """Emit CODE nodes + [:USES_CODE] edges from decisions to codes."""
    job_id = state.get("job_id", "")
    codes = state.get("detected_codes") or []
    if not codes:
        return {}

    code_brief = []
    for c in codes[:80]:
        if not isinstance(c, dict):
            continue
        code_brief.append({
            "value":   c.get("raw_value") or c.get("code") or c.get("value", ""),
            "system":  c.get("code_system") or c.get("type") or c.get("code_type", ""),
            "context": (c.get("context_snippet") or c.get("context", ""))[:200],
            "source":  c.get("source_field", ""),
        })

    # Read prior decisions from Redis to ground USES_CODE links
    decisions_payload = ctx_read(cfg, job_id, "decisions",
                                 default={"nodes": [], "edges": []}) or {}
    decision_keys = [n.get("key") for n in decisions_payload.get("nodes", [])
                     if n.get("type") == "DECISION"]

    prompt = f"""Build the CODE sub-graph for this SOP.

For each unique code emit a CODE node with key="code_<system>_<value>"
(replace any spaces with underscores). CODE.details: code_value, code_system,
description, context, confidence.

Connect each CODE to "doc" via [:HAS_CODE_REF {{code_system}}].

THEN — using your understanding of the decision-row contexts — emit
[:USES_CODE] edges from DECISION nodes (keys provided below) to the CODE
nodes they reference in their action text. Only emit USES_CODE for codes
clearly applied by that decision. Do NOT invent decision keys; only use those
listed.

Return STRICT JSON: {{"nodes":[...], "edges":[...]}}

Decision keys available:
{json.dumps(decision_keys, indent=2)[:3000]}

Codes detected:
{json.dumps(code_brief, indent=2)[:8000]}
"""

    result = _llm_call(cfg, prompt, fallback={"nodes": [], "edges": []},
                       agent_name="agent_code_grounder",
                       provider="openai",
                       expected_type=dict,
                       required_keys=["nodes", "edges"],
                       stage="graph_synthesis_stage",
                       max_tokens=6144)
    if isinstance(result, dict):
        ctx_write(cfg, job_id, "codes", result)
        ctx_audit_trail(cfg, job_id, "agent_code_grounder", "OK",
                        f"{len(result.get('nodes', []))} codes, "
                        f"{len(result.get('edges', []))} edges")
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Agent 6 — Reference Resolver  (GPT-4o)
# ──────────────────────────────────────────────────────────────────────────────

def agent_reference_resolver(state: "PipelineState",
                             cfg: "PipelineConfig") -> dict:
    """Emit REFERENCE / GROUP_LIMIT / DATE_COND / ANNOTATION nodes."""
    job_id = state.get("job_id", "")

    refs = state.get("links") or state.get("references") or []
    grps = state.get("group_rules") or []
    dates = state.get("detected_date_conditions") or []
    anns = state.get("annotations") or []

    if not (refs or grps or dates or anns):
        return {}

    inputs = {
        "references": [
            {"href": (r.get("href") or r.get("url") or r.get("ref_url", ""))[:300],
             "text": (r.get("link_text") or r.get("text") or r.get("ref_text", ""))[:200],
             "type": r.get("link_type") or r.get("ref_type", "")}
            for r in (refs[:50] if isinstance(refs, list) else [])
            if isinstance(r, dict)
        ],
        "group_rules": [
            {"name":     g.get("group_name") or g.get("name", ""),
             "inn_days": g.get("inn_days"), "oon_days": g.get("oon_days"),
             "limit_days": g.get("limit_days"),
             "basis":    g.get("calculation_basis") or g.get("calculation_from"),
             "notes":    (g.get("special_notes") or [])[:5]}
            for g in (grps[:30] if isinstance(grps, list) else [])
            if isinstance(g, dict)
        ],
        "date_conditions": [
            {"date_from": str(d.get("date_from", "")),
             "date_to":   str(d.get("date_to", "")),
             "effective_date": str(d.get("effective_date", "")),
             "context": (d.get("context_text") or d.get("context", ""))[:200]}
            for d in (dates[:30] if isinstance(dates, list) else [])
            if isinstance(d, dict)
        ],
        "annotations": [
            {"type": (a.get("annotation_type") or a.get("type") or "NOTE"),
             "text": (a.get("text") or a.get("content_text", ""))[:300]}
            for a in (anns[:50] if isinstance(anns, list) else [])
            if isinstance(a, dict)
        ],
    }

    prompt = f"""Emit cross-reference, code-list, group-limit, date-cond, and
doc-level annotation nodes attached to "doc".

TARGET STRUCTURE EXAMPLE (match the rel-types exactly):

  doc
    ├── [:HAS_CODE_REF] "EOB Codes List"
    ├── [:HAS_CODE_REF] "Medicare Reason Codes"
    ├── [:HAS_CODE_REF] "UM Service Group Code Glossary"
    └── [:REFERENCES]   "Cross-Billing Prevailing Code List"
        [:REFERENCES]   "Facets Claim Attachment Validation"
        [:REFERENCES]   "Facets Warning Message Resolution"

YOUR TASK:
  • REFERENCE nodes (cross-SOP links):
       key   = "ref_<n>"  label = the human-readable SOP title
       edge  = doc -[:REFERENCES]-> ref_<n>
       Use this for links that look like another SOP / policy document.
  • When a reference clearly points to a code-list (e.g. "EOB Codes List",
    "Medicare Reason Codes", "UM Service Group Code Glossary"),
       key   = "coderef_<n>"  label = the code-list title
       edge  = doc -[:HAS_CODE_REF]-> coderef_<n>
  • GROUP_LIMIT nodes (TFL-style per-group day limits):
       key   = "grp_<n>"  label = "<group_name>: INN <inn>d / OON <oon>d"
       edge  = doc -[:HAS_GROUP_LIMIT]-> grp_<n>
  • DATE_COND nodes:
       key   = "date_<n>"  label = short description with date range
       edge  = doc -[:HAS_DATE_COND]-> date_<n>
  • Doc-level ANNOTATION nodes (not tied to a step):
       key   = "ann_doc_<n>"  label = the annotation text
       edge  = doc -[:HAS_ANNOTATION]-> ann_doc_<n>

Dedupe references by URL — emit each distinct target only once.

Return STRICT JSON: {{"nodes":[...], "edges":[...]}}

Inputs:
{json.dumps(inputs, indent=2)[:14000]}
"""

    result = _llm_call(cfg, prompt, fallback={"nodes": [], "edges": []},
                       agent_name="agent_reference_resolver",
                       provider="openai",
                       expected_type=dict,
                       required_keys=["nodes", "edges"],
                       stage="graph_synthesis_stage",
                       max_tokens=6144)
    if isinstance(result, dict):
        ctx_write(cfg, job_id, "references", result)
        ctx_audit_trail(cfg, job_id, "agent_reference_resolver", "OK",
                        f"{len(result.get('nodes', []))} nodes")
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Agent 7 — Semantic Edge Reasoner  (Claude)
# ──────────────────────────────────────────────────────────────────────────────

def agent_semantic_edge_reasoner(state: "PipelineState",
                                 cfg: "PipelineConfig") -> dict:
    """Infer cross-cutting semantic edges (OVERRIDES, IMPLIES, etc.)."""
    job_id = state.get("job_id", "")

    # Pull the full graph-so-far from Redis to give Claude full context
    sections = ["document", "pre_sections", "steps",
                "decisions", "codes", "references"]
    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    for section in sections:
        payload = ctx_read(cfg, job_id, section, default={}) or {}
        all_nodes.extend(payload.get("nodes", []))
        all_edges.extend(payload.get("edges", []))

    if not all_nodes:
        return {}

    # Compact node summary for the LLM (key + type + label only)
    node_index = [
        {"k": n.get("key"), "t": n.get("type"),
         "l": (n.get("label", "") or "")[:80],
         "dt": (n.get("details", {}) or {}).get("decision_type", "")}
        for n in all_nodes
    ]

    prompt = f"""You are a claims-audit reasoner. Below is the partially-built
knowledge graph. Reason about it and add ONLY semantic edges that capture
inferable, defensible relationships:

  • PRE_RULE  -[:OVERRIDES]->   STEP or DECISION
      (when a pre-section exception explicitly overrides standard processing)
  • DECISION  -[:IMPLIES]->     DECISION
      (when one decision logically implies/forces another in a later step)
  • PRE_SECTION -[:GUARDS]->    STEP
      (when a pre-section gates entry to a specific step)
  • REFERENCE -[:CITED_BY]->    STEP
      (when a step explicitly cites this external SOP/document)
  • GROUP_LIMIT -[:APPLIES_TO]-> STEP
      (when a group-specific limit only applies inside one step's flow)

Rules:
  • ONLY emit edges whose source AND target keys exist in the node list.
  • DO NOT emit structural edges (HAS_*) — those are already present.
  • Provide a brief one-line `label` on each edge explaining the rationale.

Return STRICT JSON: {{"edges":[{{"source":"...","target":"...","rel":"...","label":"..."}}]}}

Node index (key, type, label, decision_type):
{json.dumps(node_index, indent=2)[:16000]}
"""

    result = _llm_call(cfg, prompt, fallback={"edges": []},
                       agent_name="agent_semantic_edge_reasoner",
                       provider="anthropic",
                       expected_type=dict,
                       required_keys=["edges"],
                       stage="graph_synthesis_stage",
                       max_tokens=4096)
    if isinstance(result, dict):
        result.setdefault("nodes", [])
        ctx_write(cfg, job_id, "semantic_edges", result)
        ctx_audit_trail(cfg, job_id, "agent_semantic_edge_reasoner", "OK",
                        f"{len(result.get('edges', []))} semantic edges")
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Agent 8 — Graph Assembler  (pure code, no LLM)
# ──────────────────────────────────────────────────────────────────────────────

def agent_graph_assembler(state: "PipelineState",
                          cfg: "PipelineConfig") -> dict:
    """Merge every agent's contribution, sanitize, validate, attach to state.

    Sanitization runs BEFORE any set/dict op so malformed LLM output
    (e.g. a node whose "key" is a dict) can never crash the pipeline.

    On validation failure → run the deterministic builder as a guard-rail
    fallback so PG/Neo4j writers ALWAYS have a graph to persist.
    """
    job_id = state.get("job_id", "")

    raw_nodes: list = []
    raw_edges: list = []
    sections = ["document", "pre_sections", "steps", "decisions",
                "codes", "references", "semantic_edges"]
    for section in sections:
        payload = ctx_read(cfg, job_id, section, default={}) or {}
        if isinstance(payload, dict):
            raw_nodes.extend(payload.get("nodes") or [])
            raw_edges.extend(payload.get("edges") or [])

    sanitized_nodes, sanitized_edges = sanitize_graph(raw_nodes, raw_edges)
    dropped_n = len(raw_nodes) - len(sanitized_nodes)
    dropped_e = len(raw_edges) - len(sanitized_edges)
    if dropped_n or dropped_e:
        log.warning("agent_graph_assembler: sanitizer dropped %d malformed "
                    "nodes, %d malformed edges", dropped_n, dropped_e)

    merged_nodes: dict[str, dict] = {}
    for n in sanitized_nodes:
        merged_nodes.setdefault(n["key"], n)
    nodes = list(merged_nodes.values())
    edges = sanitized_edges

    validation = _validate_graph(nodes, edges)
    hard, soft, bad_idx = validation["hard"], validation["soft"], validation["bad_edge_idx"]

    if hard:
        # Structural failure → can't trust the agentic graph, fall back.
        log.warning("agent_graph_assembler: %d HARD validation issues — "
                    "falling back to deterministic builder. First: %s",
                    len(hard), hard[0])
        ctx_audit_trail(cfg, job_id, "agent_graph_assembler",
                        "FALLBACK_DETERMINISTIC",
                        f"hard: {hard[:3]}")
        from ..graph_builder import build_audit_graph
        nodes, edges = build_audit_graph(state)
        nodes, edges = sanitize_graph(nodes, edges)
        validation = _validate_graph(nodes, edges)
        bad_idx = validation["bad_edge_idx"]
        soft = validation["soft"]

    # Soft errors = dangling edges. Drop them and keep the rest of the
    # agentic graph — preserves the LLM's semantic richness.
    clean_edges = [e for i, e in enumerate(edges) if i not in bad_idx]
    dropped_dangling = len(edges) - len(clean_edges)
    if dropped_dangling:
        log.warning("agent_graph_assembler: dropped %d dangling edges "
                    "(SOFT). Example: %s", dropped_dangling, soft[0])

    log.info("agent_graph_assembler: assembled %d nodes, %d edges "
             "(dropped %d dangling, %d malformed pre-sanitize)",
             len(nodes), len(clean_edges), dropped_dangling, dropped_n + dropped_e)
    ctx_audit_trail(cfg, job_id, "agent_graph_assembler", "OK",
                    f"{len(nodes)} nodes, {len(clean_edges)} edges")

    return {
        "audit_graph_nodes": nodes,
        "audit_graph_edges": clean_edges,
    }
