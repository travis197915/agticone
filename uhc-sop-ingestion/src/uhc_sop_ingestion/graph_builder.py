"""
graph_builder.py — Canonical SOP Knowledge-Graph Builder

Reads the in-memory pipeline ``state`` (parsed + enriched + context data)
and emits a list of typed nodes and directed edges that form the
canonical SOP knowledge graph:

    (:SopDocument)                                         ← GOD NODE
      ├─ [:HAS_META]            → (:Meta)
      ├─ [:HAS_PRE_SECTION]     → (:PreSection)
      │     └─ [:HAS_RULE]      → (:PreRule)
      ├─ [:HAS_STEP]            → (:Step)
      │     ├─ [:HAS_DECISION]  → (:Decision)
      │     │     └─ [:GOTO]    → (:Step)
      │     └─ [:HAS_ANNOTATION]→ (:Annotation)
      ├─ [:HAS_GROUP_LIMIT]     → (:GroupLimit)
      ├─ [:HAS_CODE_REF]        → (:Code)
      ├─ [:HAS_DATE_COND]       → (:DateCondition)
      └─ [:REFERENCES]          → (:Reference)

The same structure is materialised by:
  • a11_write_postgres.pg_graph_writer   (PostgreSQL: AuditGraphNode + AuditGraphEdge)
  • a10_write_neo4j.neo4j_graph_writer   (Neo4j: native nodes + relationships)

so the viewer (and any downstream audit reasoner) can read from either store
and get the exact same graph.
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .html_dom import block_id_for_html

if TYPE_CHECKING:
    from .state import PipelineState


def _src_bid(*candidates: Any) -> str:
    """Return the content-hashed HtmlBlock id for the first non-empty source_html.

    Used to plumb DOM-mirror provenance into ``GraphNode.details.source_block_id``
    so :func:`a10_write_neo4j.neo4j_graph_writer` can emit
    ``(:GraphNode)-[:DERIVED_FROM]->(:HtmlBlock)`` cross-edges.
    """
    for c in candidates:
        if c and isinstance(c, str):
            return block_id_for_html(c)
    return ""


# ─── Constants ──────────────────────────────────────────────────────────────

NODE_TYPES = (
    "DOCUMENT", "META", "PRE_SECTION", "PRE_RULE",
    "STEP", "DECISION", "ANNOTATION",
    "GROUP_LIMIT", "CODE", "DATE_COND", "REFERENCE",
)

EDGE_TYPES = (
    "HAS_META", "HAS_PRE_SECTION", "HAS_RULE",
    "HAS_STEP", "HAS_DECISION", "HAS_ANNOTATION",
    "HAS_GROUP_LIMIT", "HAS_CODE_REF", "HAS_DATE_COND",
    "REFERENCES", "GOTO",
)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _trim(s: Any, n: int = 80) -> str:
    s = ("" if s is None else str(s)).strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _classify_pre_rule(rule: dict) -> str:
    """Best-effort decision-type classification for an LLM-extracted pre-section rule."""
    if not isinstance(rule, dict):
        return "NOTE"
    dt = (rule.get("decision_type") or rule.get("rule_type") or "").upper()
    if dt in ("DENY", "ALLOW", "BYPASS", "OVERRIDE", "ELIGIBILITY",
              "REFER", "NOTE", "WAIVE", "PEND"):
        return dt
    if rule.get("is_exception"):
        return "OVERRIDE"
    text = (rule.get("action", "") + " " + rule.get("condition", "")).upper()
    if "DENY"   in text: return "DENY"
    if "ALLOW"  in text: return "ALLOW"
    if "BYPASS" in text or "OVERRIDE" in text: return "BYPASS"
    if "PEND"   in text: return "PEND"
    if "WAIVE"  in text: return "WAIVE"
    return "NOTE"


# ─── Main builder ───────────────────────────────────────────────────────────

def build_audit_graph(state: "PipelineState") -> tuple[list[dict], list[dict]]:
    """Build the canonical knowledge graph for the SOP currently in `state`.

    Returns:
        (nodes, edges) — list of dicts. Each node has:
          { key, type, label, details (dict), ref_table, ref_id, order }
        Each edge has:
          { source, target, rel, label, details (dict) }
    """
    meta   = state.get("metadata") or {}
    url    = state.get("current_url", "") or state.get("seed_url", "")
    h      = state.get("content_hash", "")
    title  = meta.get("title", "") or _trim(url, 100) or "SOP"

    pre_secs = state.get("pre_sections") or []
    steps    = state.get("enriched_steps") or state.get("steps") or []
    codes    = state.get("detected_codes") or []
    grps     = state.get("group_rules") or []
    dates    = (state.get("detected_date_conditions")
                or state.get("date_conditions") or [])
    anns     = state.get("annotations") or []
    refs     = (state.get("references") or state.get("ref_links")
                or state.get("links") or [])

    nodes: list[dict] = []
    edges: list[dict] = []
    seen_keys: set[str] = set()

    def add_node(key: str, ntype: str, label: str, *,
                 details: dict | None = None,
                 ref_table: str = "", ref_id: int | None = None,
                 order: int = 0) -> None:
        if key in seen_keys:
            return
        seen_keys.add(key)
        nodes.append({
            "key": key,
            "type": ntype,
            "label": label[:255],
            "details": details or {},
            "ref_table": ref_table,
            "ref_id": ref_id,
            "order": order,
        })

    def add_edge(src: str, tgt: str, rel: str,
                 label: str = "", details: dict | None = None) -> None:
        edges.append({
            "source": src, "target": tgt,
            "rel": rel, "label": label[:255],
            "details": details or {},
        })

    # 1. SopDocument — the GOD node ─────────────────────────────────────────
    add_node("doc", "DOCUMENT", _trim(title, 200),
             ref_table="auditsop",
             details={
                 "url": url,
                 "content_hash": h,
                 "platform": meta.get("platform", ""),
                 "effective_date": str(meta.get("effective_date", "")),
                 "revision_date":  str(meta.get("revision_date", "")),
                 "lob": meta.get("lob", []),
                 "audience": meta.get("audience", []),
                 "summary": (state.get("llm_summary") or meta.get("summary", ""))[:600],
             })

    # 2. Metadata node ─────────────────────────────────────────────────────
    add_node("meta", "META", "Document Metadata",
             details={
                 "platform": meta.get("platform", ""),
                 "effective_date": str(meta.get("effective_date", "")),
                 "revision_date":  str(meta.get("revision_date", "")),
                 "state_div": meta.get("state_div", ""),
                 "product":   meta.get("product", ""),
                 "doc_format": state.get("doc_format", ""),
                 "crawl_depth": state.get("current_depth", 0),
             })
    add_edge("doc", "meta", "HAS_META")

    # 3. Pre-sections + their LLM-extracted rules ──────────────────────────
    for i, ps in enumerate(pre_secs):
        name = ps.get("name", "") or f"Pre-Section {i + 1}"
        items = ps.get("items", [])
        content = "\n".join(
            (it.get("text", str(it)) if isinstance(it, dict) else str(it))
            for it in items
        )
        pkey = f"pre_{i}"
        add_node(pkey, "PRE_SECTION", _trim(name, 200),
                 order=i,
                 details={
                     "name": name,
                     "order": i,
                     "section_id": ps.get("section_id", ""),
                     "content": content[:800],
                     "is_exception_block": bool(ps.get("is_exception_block")),
                     "source_block_id": _src_bid(ps.get("source_html")),
                 })
        add_edge("doc", pkey, "HAS_PRE_SECTION",
                 label=f"order:{i}", details={"order": i})

        # llm_rules — one PRE_RULE node per extracted rule
        for ridx, rule in enumerate(ps.get("llm_rules", []) or []):
            if not isinstance(rule, dict):
                continue
            cond = rule.get("condition", "") or rule.get("text", "")
            if not cond:
                continue
            rkey = f"{pkey}_r{ridx}"
            rtype = _classify_pre_rule(rule)
            add_node(rkey, "PRE_RULE", _trim(cond, 200),
                     details={
                         "condition": cond,
                         "action": rule.get("action", ""),
                         "decision_type": rtype,
                         "is_exception": bool(rule.get("is_exception")),
                         "rule_type": rule.get("rule_type", ""),
                     })
            add_edge(pkey, rkey, "HAS_RULE", label=rtype,
                     details={"decision_type": rtype})

    # 4. Steps + their decisions + annotations + GOTO edges ─────────────────
    step_num_to_key: dict[int, str] = {}
    for step in steps:
        try:
            num = int(step.get("number", step.get("step_number", -1)))
        except (TypeError, ValueError):
            continue
        if num < 0:
            continue
        skey = f"step_{num}"
        step_num_to_key[num] = skey
        is_term = bool(step.get("is_terminal", False))
        q = step.get("question", "") or step.get("title", "") or f"Step {num}"
        add_node(skey, "STEP",
                 _trim(f"Step {num}: {q}", 200),
                 order=num,
                 details={
                     "step_number": num,
                     "question": q,
                     "intro": step.get("intro_text", ""),
                     "is_terminal": is_term,
                     "is_sub_procedure": bool(step.get("is_sub_procedure")),
                     "sub_procedure_name": step.get("sub_procedure_name", ""),
                     "terminal_action": step.get("terminal_action", ""),
                     "source_block_id": _src_bid(step.get("source_html")),
                 })
        add_edge("doc", skey, "HAS_STEP",
                 label=f"order:{num}", details={"order": num})

    # Now wire decisions (need step keys to exist for GOTO targets)
    for step in steps:
        try:
            num = int(step.get("number", step.get("step_number", -1)))
        except (TypeError, ValueError):
            continue
        skey = step_num_to_key.get(num)
        if not skey:
            continue
        decision_rows = step.get("decision_rows") or step.get("rows") or []
        for didx, dec in enumerate(decision_rows):
            if not isinstance(dec, dict):
                continue
            cond = (dec.get("condition_if") or dec.get("if")
                    or dec.get("condition", ""))
            act  = (dec.get("action_text") or dec.get("then")
                    or dec.get("action", ""))
            # parser writes `decision` (DENY/ALLOW/…), enricher writes `decision_type`
            dtype = (dec.get("decision_type") or dec.get("decision")
                     or _classify_pre_rule({"condition": cond, "action": act})).upper()
            # parser writes `skip_to_step`, enricher may write `goto_step`
            goto = dec.get("goto_step") or dec.get("skip_to_step")
            if goto is not None:
                try:
                    goto = int(goto)
                except (TypeError, ValueError):
                    goto = None
            dec_codes = dec.get("codes") or []
            # codes from parser is a flat list like ["E51","003","CDD"];
            # classify them into eob/ex/denial buckets the viewer expects
            eob_list  = dec.get("eob_codes") or [c for c in dec_codes if str(c).upper().startswith(("E","F","W"))]
            ex_list   = dec.get("ex_codes")  or [c for c in dec_codes if str(c).isdigit()]
            den_list  = dec.get("denial_codes") or [c for c in dec_codes if str(c).upper() in {"CDD","CDS","CDA"}]
            dkey = f"step_{num}_d{didx}"
            add_node(dkey, "DECISION", _trim(cond or "(implicit)", 200),
                     details={
                         "condition_if": cond,
                         "condition_and": dec.get("condition_and", ""),
                         "action_text": act,
                         "decision_type": dtype,
                         "eob_codes":    eob_list,
                         "ex_codes":     ex_list,
                         "denial_codes": den_list,
                         "goto_step":    goto,
                         "is_final":     bool(dec.get("is_final")
                                              or dec.get("is_terminal")),
                         "source_block_id": _src_bid(
                             dec.get("source_html"), step.get("source_html"),
                         ),
                     })
            add_edge(skey, dkey, "HAS_DECISION", label=dtype,
                     details={"decision_type": dtype})
            if goto and goto in step_num_to_key:
                add_edge(dkey, step_num_to_key[goto], "GOTO",
                         label=f"→ Step {goto}",
                         details={"target_step": goto})

        # Step-level annotations (notes/alerts)
        for aidx, ann in enumerate(step.get("annotations", []) or []):
            if not isinstance(ann, dict):
                ann = {"text": str(ann), "annotation_type": "NOTE"}
            akey = f"step_{num}_a{aidx}"
            atype = (ann.get("annotation_type") or "NOTE").upper()
            text = ann.get("text") or ann.get("content_text", "")
            if not text:
                continue
            add_node(akey, "ANNOTATION", _trim(text, 200),
                     details={
                         "text": text,
                         "annotation_type": atype,
                         "is_claim_impact": bool(ann.get("is_claim_impact")),
                     })
            add_edge(skey, akey, "HAS_ANNOTATION", label=atype,
                     details={"annotation_type": atype})

    # 5. Global annotations (not tied to a step) ───────────────────────────
    for aidx, ann in enumerate(anns):
        if not isinstance(ann, dict):
            ann = {"text": str(ann), "annotation_type": "NOTE"}
        if ann.get("step_number") is not None:
            continue  # already attached above
        text = ann.get("text") or ann.get("content_text", "")
        if not text:
            continue
        akey = f"ann_g{aidx}"
        atype = (ann.get("annotation_type") or "NOTE").upper()
        add_node(akey, "ANNOTATION", _trim(text, 200),
                 details={
                     "text": text, "annotation_type": atype,
                     "is_claim_impact": bool(ann.get("is_claim_impact")),
                 })
        add_edge("doc", akey, "HAS_ANNOTATION", label=atype,
                 details={"annotation_type": atype})

    # 6. Group limits ──────────────────────────────────────────────────────
    for gidx, grp in enumerate(grps):
        if not isinstance(grp, dict):
            continue
        gname = grp.get("group_name", "") or f"Group {gidx + 1}"
        inn  = grp.get("inn_days") or grp.get("provider_days")
        oon  = grp.get("oon_days") or grp.get("oon_provider_days")
        lim  = grp.get("limit_days") or grp.get("days")
        gkey = f"grp_{gidx}"
        add_node(gkey, "GROUP_LIMIT",
                 _trim(f"{gname} (INN {inn or '—'}/OON {oon or '—'}/Limit {lim or '—'})", 200),
                 details={
                     "group_name": gname,
                     "inn_days": inn, "oon_days": oon, "limit_days": lim,
                     "basis": grp.get("calculation_basis") or grp.get("basis", ""),
                     "member_submitted_only": bool(grp.get("member_submitted_only")),
                     "special_notes": grp.get("special_notes", []) or [],
                     "exceptions": grp.get("exceptions", []) or [],
                 })
        add_edge("doc", gkey, "HAS_GROUP_LIMIT")

    # 7. Codes ─────────────────────────────────────────────────────────────
    seen_codes: set[str] = set()
    for cidx, code in enumerate(codes):
        if not isinstance(code, dict):
            continue
        cval = (code.get("raw_value") or code.get("code")
                or code.get("value") or code.get("code_value", ""))
        if not cval:
            continue
        ctype = (code.get("code_system") or code.get("type")
                 or code.get("code_type") or code.get("category")
                 or "UNKNOWN").upper()
        key = f"{ctype}:{cval}"
        if key in seen_codes:
            continue
        seen_codes.add(key)
        ckey = f"code_{ctype}_{cval}".replace(" ", "_")
        add_node(ckey, "CODE", f"{ctype}: {cval}",
                 details={
                     "code_type": ctype,
                     "code_value": cval,
                     "description": code.get("description", ""),
                     "context": (code.get("context_snippet")
                                 or code.get("context", ""))[:300],
                     "confidence": code.get("confidence", 1.0),
                     "source_step": code.get("source_step"),
                 })
        add_edge("doc", ckey, "HAS_CODE_REF", label=ctype,
                 details={"code_type": ctype})

    # 8. Date conditions ───────────────────────────────────────────────────
    for didx, dc in enumerate(dates):
        if not isinstance(dc, dict):
            continue
        parts = []
        if dc.get("date_from"):      parts.append(f"From {dc['date_from']}")
        if dc.get("date_to"):        parts.append(f"To {dc['date_to']}")
        if dc.get("effective_date"): parts.append(f"Eff {dc['effective_date']}")
        label = " / ".join(parts) or "Date Condition"
        dkey2 = f"date_{didx}"
        add_node(dkey2, "DATE_COND", _trim(label, 200),
                 details={
                     "date_from": str(dc.get("date_from", "")),
                     "date_to":   str(dc.get("date_to", "")),
                     "effective_date": str(dc.get("effective_date", "")),
                     "applies_to": dc.get("applies_to", ""),
                     "context": dc.get("context_text") or dc.get("context", ""),
                 })
        add_edge("doc", dkey2, "HAS_DATE_COND")

    # 9. Cross-references to other SOPs ────────────────────────────────────
    seen_refs: set[str] = set()
    for ridx, ref in enumerate(refs):
        if not isinstance(ref, dict):
            ref = {"text": str(ref)}
        text = (ref.get("text") or ref.get("ref_text")
                or ref.get("link_text", ""))
        url2 = (ref.get("url") or ref.get("ref_url")
                or ref.get("href", ""))
        if not (text or url2):
            continue
        # Deduplicate references — many anchor links point to the same target
        ref_id = url2 or text
        if ref_id in seen_refs:
            continue
        seen_refs.add(ref_id)
        rkey = f"ref_{ridx}"
        rtype = ref.get("ref_type") or ref.get("link_type", "UNRESOLVED")
        add_node(rkey, "REFERENCE", _trim(text or url2, 200),
                 details={
                     "ref_text": text,
                     "ref_url": url2,
                     "ref_type": rtype,
                     "is_resolved": bool(ref.get("is_resolved")
                                         or ref.get("resolved_url")
                                         or ref.get("status") == "OK"),
                 })
        add_edge("doc", rkey, "REFERENCES", label=rtype,
                 details={"ref_type": rtype})

    return nodes, edges
