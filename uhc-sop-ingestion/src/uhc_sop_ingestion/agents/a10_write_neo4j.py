"""NEO4J WRITE LAYER — 10 agents.

All writes use MERGE (idempotent). Re-ingesting the same content hash
is safe — properties are updated in-place.

 1. GodNodeAgent          — (:SopDocument) god node
 2. PreSectionNodeAgent   — (:PreSection) nodes + HAS_PRE_SECTION edges
 3. StepNodeAgent         — (:Step) nodes + HAS_STEP edges
 4. RuleNodeAgent         — (:Rule) nodes + HAS_RULE edges
 5. AnnotationNodeAgent   — (:Annotation) nodes + ANNOTATED_BY edges
 6. CodeNodeAgent         — (:Code) nodes + USES_CODE edges
 7. GroupRuleNodeAgent    — (:GroupRule) nodes + HAS_GROUP_RULE edges
 8. SequentialEdgeAgent   — NEXT_STEP edges between consecutive steps
 9. BranchEdgeAgent       — BRANCH_YES / BRANCH_NO / SKIPS_TO edges
10. ChildDocEdgeAgent     — LINKS_TO edges to child SopDocuments
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import PipelineState
    from ..config import PipelineConfig

logger = logging.getLogger(__name__)


def _driver(cfg):
    from ..config import get_neo4j
    return get_neo4j(cfg)

def _sop_id(url: str, content_hash: str) -> str:
    stem = url.rsplit("/",1)[-1].rsplit(".",1)[0] or "sop"
    return f"{stem}:{content_hash}"

def _run(session, query: str, params: dict):
    try:
        session.execute_write(lambda tx: tx.run(query, params))
    except Exception as e:
        logger.error("neo4j write error: %s | %s", e, query[:80])


# ── 1. GodNodeAgent ──────────────────────────────────────────────────────────

def god_node_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    url  = state.get("current_url","")
    h    = state.get("content_hash","")
    meta = state.get("metadata") or {}
    sop_id = _sop_id(url, h)
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            _run(s, """
                MERGE (d:SopDocument {sop_id:$sop_id})
                SET d.title=$title, d.url=$url, d.content_hash=$h,
                    d.doc_format=$fmt, d.effective_date=$eff,
                    d.revision_date=$rev, d.platform=$platform,
                    d.crawl_depth=$depth, d.is_current=true,
                    d.updated_at=datetime()
            """, {"sop_id":sop_id,"title":meta.get("title",""),"url":url,
                  "h":h,"fmt":state.get("doc_format",""),
                  "eff":meta.get("effective_date",""),
                  "rev":meta.get("revision_date",""),
                  "platform":meta.get("platform",""),
                  "depth":state.get("current_depth",0)})
    except Exception as e:
        return {"errors":[{"agent":"GodNodeAgent","msg":str(e)}], "neo4j_sop_id": sop_id}
    return {"neo4j_sop_id": sop_id}


# ── 2. PreSectionNodeAgent ────────────────────────────────────────────────────

def pre_section_node_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    sop_id = state.get("neo4j_sop_id","")
    if not sop_id: return {}
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            for i, ps in enumerate(state.get("pre_sections") or []):
                ps_id = f"{sop_id}:pre:{i}"
                _run(s, """
                    MERGE (p:PreSection {ps_id:$ps_id})
                    SET p.name=$name, p.order=$order, p.item_count=$cnt
                    WITH p MATCH (d:SopDocument {sop_id:$sop_id})
                    MERGE (d)-[:HAS_PRE_SECTION {order:$order}]->(p)
                """, {"ps_id":ps_id,"name":ps.get("name",""),"order":i,
                      "cnt":len(ps.get("items",[])),"sop_id":sop_id})
    except Exception as e:
        return {"errors":[{"agent":"PreSectionNodeAgent","msg":str(e)}]}
    return {}


# ── 3. StepNodeAgent ──────────────────────────────────────────────────────────

def step_node_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    sop_id = state.get("neo4j_sop_id","")
    if not sop_id: return {}
    steps  = state.get("enriched_steps") or state.get("steps") or []
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            for step in steps:
                step_id = f"{sop_id}:step:{step['number']}"
                _run(s, """
                    MERGE (st:Step {step_id:$step_id})
                    SET st.number=$num, st.question=$q,
                        st.branch_yes=$yes, st.branch_no=$no,
                        st.is_terminal=$term, st.row_count=$cnt
                    WITH st MATCH (d:SopDocument {sop_id:$sop_id})
                    MERGE (d)-[:HAS_STEP {order:$num}]->(st)
                """, {"step_id":step_id,"num":step["number"],"q":step.get("question",""),
                      "yes":step.get("branch_yes",""),"no":step.get("branch_no",""),
                      "term":step.get("is_terminal",False),
                      "cnt":len(step.get("decision_rows",[])),"sop_id":sop_id})
    except Exception as e:
        return {"errors":[{"agent":"StepNodeAgent","msg":str(e)}]}
    return {}


# ── 4. RuleNodeAgent ──────────────────────────────────────────────────────────

def rule_node_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    sop_id = state.get("neo4j_sop_id","")
    if not sop_id: return {}
    steps  = state.get("enriched_steps") or state.get("steps") or []
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            for step in steps:
                step_id = f"{sop_id}:step:{step['number']}"
                for j, row in enumerate(step.get("decision_rows",[])):
                    rule_id = f"{step_id}:rule:{j}"
                    _run(s, """
                        MERGE (r:Rule {rule_id:$rule_id})
                        SET r.condition_if=$if, r.condition_and=$and,
                            r.action=$action, r.decision=$dec,
                            r.codes=$codes, r.skip_to_step=$skip,
                            r.row_index=$j
                        WITH r MATCH (st:Step {step_id:$step_id})
                        MERGE (st)-[:HAS_RULE {index:$j}]->(r)
                    """, {"rule_id":rule_id,"step_id":step_id,
                          "if":row.get("condition_if",""),"and":row.get("condition_and",""),
                          "action":row.get("action",""),"dec":row.get("decision","CONDITIONAL"),
                          "codes":row.get("codes",[]),"skip":row.get("skip_to_step"),"j":j})
    except Exception as e:
        return {"errors":[{"agent":"RuleNodeAgent","msg":str(e)}]}
    return {}


# ── 5. AnnotationNodeAgent ────────────────────────────────────────────────────

def annotation_node_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    sop_id = state.get("neo4j_sop_id","")
    if not sop_id: return {}
    steps = state.get("enriched_steps") or state.get("steps") or []
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            for step in steps:
                step_id = f"{sop_id}:step:{step['number']}"
                for ann in step.get("annotations",[]):
                    _run(s, """
                        MERGE (a:Annotation {text:$text})
                        SET a.annotation_type=$type
                        WITH a MATCH (st:Step {step_id:$step_id})
                        MERGE (st)-[:ANNOTATED_BY]->(a)
                    """, {"text":ann.get("text","")[:500],
                          "type":ann.get("annotation_type","NOTE"),"step_id":step_id})
    except Exception as e:
        return {"errors":[{"agent":"AnnotationNodeAgent","msg":str(e)}]}
    return {}


# ── 6. CodeNodeAgent ──────────────────────────────────────────────────────────

def code_node_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    sop_id = state.get("neo4j_sop_id","")
    if not sop_id: return {}
    codes = state.get("detected_codes") or []
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            for code in codes:
                code_id = f"code:{code['code_system']}:{code['raw_value']}"
                _run(s, """
                    MERGE (c:Code {code_id:$code_id})
                    SET c.raw_value=$val, c.code_system=$sys, c.confidence=$conf
                    WITH c MATCH (d:SopDocument {sop_id:$sop_id})
                    MERGE (d)-[:USES_CODE {confidence:$conf}]->(c)
                """, {"code_id":code_id,"val":code["raw_value"],"sys":code["code_system"],
                      "conf":code.get("confidence",1.0),"sop_id":sop_id})
    except Exception as e:
        return {"errors":[{"agent":"CodeNodeAgent","msg":str(e)}]}
    return {}


# ── 7. GroupRuleNodeAgent ─────────────────────────────────────────────────────

def group_rule_node_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    sop_id = state.get("neo4j_sop_id","")
    if not sop_id: return {}
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            for gr in (state.get("group_rules") or []):
                gr_id = f"{sop_id}:gr:{gr.get('group_name','')}:{gr.get('network_type','')}"
                _run(s, """
                    MERGE (g:GroupRule {gr_id:$gr_id})
                    SET g.group_name=$name, g.network_type=$net,
                        g.limit_days=$days, g.limit_months=$months,
                        g.calculation_from=$from, g.member_only=$mo
                    WITH g MATCH (d:SopDocument {sop_id:$sop_id})
                    MERGE (d)-[:HAS_GROUP_RULE]->(g)
                """, {"gr_id":gr_id,"name":gr.get("group_name",""),
                      "net":gr.get("network_type","BOTH"),
                      "days":gr.get("limit_days"),"months":gr.get("limit_months"),
                      "from":gr.get("calculation_from","DOS"),
                      "mo":gr.get("member_submitted_only",False),"sop_id":sop_id})
    except Exception as e:
        return {"errors":[{"agent":"GroupRuleNodeAgent","msg":str(e)}]}
    return {}


# ── 8. SequentialEdgeAgent ────────────────────────────────────────────────────

def sequential_edge_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    sop_id = state.get("neo4j_sop_id","")
    if not sop_id: return {}
    steps  = sorted(state.get("enriched_steps") or state.get("steps") or [],
                    key=lambda s: s["number"])
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            for i in range(len(steps)-1):
                from_id = f"{sop_id}:step:{steps[i]['number']}"
                to_id   = f"{sop_id}:step:{steps[i+1]['number']}"
                _run(s, """
                    MATCH (a:Step {step_id:$from_id})
                    MATCH (b:Step {step_id:$to_id})
                    MERGE (a)-[:NEXT_STEP]->(b)
                """, {"from_id":from_id,"to_id":to_id})
    except Exception as e:
        return {"errors":[{"agent":"SequentialEdgeAgent","msg":str(e)}]}
    return {}


# ── 9. BranchEdgeAgent ───────────────────────────────────────────────────────

def branch_edge_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    sop_id = state.get("neo4j_sop_id","")
    if not sop_id: return {}
    steps = state.get("enriched_steps") or state.get("steps") or []
    step_nums = {s["number"] for s in steps}
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            for step in steps:
                from_id = f"{sop_id}:step:{step['number']}"
                for target, rel in [
                    (step.get("skip_to_step_yes"), "BRANCH_YES"),
                    (step.get("skip_to_step_no"),  "BRANCH_NO"),
                ]:
                    if target and target in step_nums:
                        _run(s, f"""
                            MATCH (a:Step {{step_id:$f}})
                            MATCH (b:Step {{step_id:$t}})
                            MERGE (a)-[:{rel}]->(b)
                        """, {"f":from_id,"t":f"{sop_id}:step:{target}"})
                for row in step.get("decision_rows",[]):
                    skip = row.get("skip_to_step")
                    if skip and skip in step_nums:
                        _run(s, """
                            MATCH (a:Step {step_id:$f})
                            MATCH (b:Step {step_id:$t})
                            MERGE (a)-[:SKIPS_TO]->(b)
                        """, {"f":from_id,"t":f"{sop_id}:step:{skip}"})
    except Exception as e:
        return {"errors":[{"agent":"BranchEdgeAgent","msg":str(e)}]}
    return {}


# ── 10. ChildDocEdgeAgent ────────────────────────────────────────────────────

def child_doc_edge_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    sop_id  = state.get("neo4j_sop_id","")
    parent  = state.get("current_parent_url","")
    if not sop_id or not parent: return {}
    parent_doc = next((d for d in (state.get("all_documents") or []) if d.get("url")==parent), None)
    if not parent_doc: return {}
    parent_sop_id = parent_doc.get("neo4j_sop_id","")
    if not parent_sop_id: return {}
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            # [:REFERENCES] for named SOP cross-references; [:LINKS_TO] for
            # generic URL links discovered via href scraping
            _run(s, """
                MATCH (root:SopDocument {sop_id:$parent_id})
                MATCH (child:SopDocument {sop_id:$child_id})
                MERGE (root)-[:REFERENCES {depth:$depth}]->(child)
            """, {"parent_id":parent_sop_id,"child_id":sop_id,
                  "depth":state.get("current_depth",1)})
    except Exception as e:
        return {"errors":[{"agent":"ChildDocEdgeAgent","msg":str(e)}]}
    return {}


# ── 11. ReferenceTableNodeAgent ──────────────────────────────────────────────

def reference_table_node_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Writes (:ReferenceTable) nodes (Valid POTF, Invalid POTF, Code tables)
    with [:HAS_REFERENCE_TABLE] edges from the god node.
    Each item in the table becomes an [:HAS_ITEM]->(:RefItem) node.
    """
    sop_id = state.get("neo4j_sop_id","")
    if not sop_id: return {}
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            for rt in (state.get("reference_tables") or []):
                rt_id  = f"{sop_id}:reftbl:{rt.get('name','')[:40]}"
                _run(s, """
                    MERGE (r:ReferenceTable {rt_id:$rt_id})
                    SET r.name=$name, r.table_type=$ttype
                    WITH r MATCH (d:SopDocument {sop_id:$sop_id})
                    MERGE (d)-[:HAS_REFERENCE_TABLE]->(r)
                """, {"rt_id":rt_id,"name":rt.get("name",""),
                      "ttype":rt.get("table_type","LOOKUP"),"sop_id":sop_id})
                for i, row in enumerate(rt.get("rows",[])):
                    item_id = f"{rt_id}:item:{i}"
                    _run(s, """
                        MERGE (it:RefItem {item_id:$item_id})
                        SET it.content=$content, it.row_type=$rtype
                        WITH it MATCH (r:ReferenceTable {rt_id:$rt_id})
                        MERGE (r)-[:HAS_ITEM {order:$ord}]->(it)
                    """, {"item_id":item_id,"content":row.get("content","")[:500],
                          "rtype":row.get("row_type","ITEM"),"rt_id":rt_id,"ord":i})
    except Exception as e:
        return {"errors":[{"agent":"ReferenceTableNodeAgent","msg":str(e)}]}
    return {}


# ── 12. GroupRuleStepEdgeAgent ────────────────────────────────────────────────

def group_rule_step_edge_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Writes (:Step)-[:HAS_GROUP_RULE]->(:GroupRule) edges so that group
    rules discovered inside a step cell (Step 4) are properly linked to
    their parent step — not just to the SopDocument god node.
    """
    sop_id = state.get("neo4j_sop_id","")
    if not sop_id: return {}
    steps  = state.get("enriched_steps") or state.get("steps") or []
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            for step in steps:
                group_rows = step.get("group_rows") or []
                if not group_rows: continue
                step_id = f"{sop_id}:step:{step['number']}"
                for gr in group_rows:
                    gr_id = f"{sop_id}:step:{step['number']}:group:{gr.get('group_name','')}"
                    _run(s, """
                        MERGE (g:GroupRule {gr_id:$gr_id})
                        SET g.group_name=$name, g.raw_text=$raw, g.network_type=$net,
                            g.is_highlighted=$hl
                        WITH g
                        MATCH (st:Step {step_id:$step_id})
                        MERGE (st)-[:HAS_GROUP_RULE]->(g)
                    """, {"gr_id":gr_id,"name":gr.get("group_name",""),
                          "raw":gr.get("raw_text","")[:500],
                          "net":gr.get("network_type","BOTH"),
                          "hl":gr.get("is_highlighted",False),
                          "step_id":step_id})
    except Exception as e:
        return {"errors":[{"agent":"GroupRuleStepEdgeAgent","msg":str(e)}]}
    return {}


# ── 12. SubProcedureNodeAgent ─────────────────────────────────────────────────

def sub_procedure_node_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Writes sub-procedures (ERB) as (:SubProcedure) nodes with their
    own (:Step) children, linked to the god node via [:HAS_SUB_PROCEDURE].
    """
    sop_id = state.get("neo4j_sop_id","")
    if not sop_id: return {}
    try:
        with _driver(cfg).session(database=cfg.neo4j_database) as s:
            for sp in (state.get("sub_procedures") or []):
                sp_id = f"{sop_id}:sub:{sp.get('name','')[:40]}"
                _run(s, """
                    MERGE (p:SubProcedure {sp_id:$sp_id})
                    SET p.name=$name, p.entry_condition=$entry
                    WITH p
                    MATCH (d:SopDocument {sop_id:$sop_id})
                    MERGE (d)-[:HAS_SUB_PROCEDURE]->(p)
                """, {"sp_id":sp_id,"name":sp.get("name",""),
                      "entry":sp.get("entry_condition",""),"sop_id":sop_id})
                # Write ERB steps under the sub-procedure node
                for step in (sp.get("steps") or []):
                    step_id = f"{sp_id}:step:{step['number']}"
                    _run(s, """
                        MERGE (st:Step {step_id:$step_id})
                        SET st.number=$num, st.question=$q, st.is_terminal=$term
                        WITH st
                        MATCH (p:SubProcedure {sp_id:$sp_id})
                        MERGE (p)-[:HAS_STEP {order:$num}]->(st)
                    """, {"step_id":step_id,"num":step["number"],
                          "q":step.get("question",""),"term":step.get("is_terminal",False),
                          "sp_id":sp_id})
    except Exception as e:
        return {"errors":[{"agent":"SubProcedureNodeAgent","msg":str(e)}]}
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# neo4j_graph_writer — mirror the canonical SOP knowledge graph into Neo4j
#
# Uses the same shared builder as pg_graph_writer so the structure is identical
# in both stores: SopDocument GOD-node with PRE_SECTION / STEP / DECISION /
# CODE / GROUP_LIMIT / REFERENCE / DATE_COND / ANNOTATION children and GOTO
# edges between decisions and their target steps.
#
# Every node carries `sop_id` + `node_key` so re-ingestion is idempotent
# (MERGE keys), and the whole sub-graph is wiped first to drop stale rows.
# ─────────────────────────────────────────────────────────────────────────────

# node_type → Neo4j label
_TYPE_LABEL = {
    "DOCUMENT":    "SopDocument",
    "META":        "Metadata",
    "PRE_SECTION": "PreSection",
    "PRE_RULE":    "PreRule",
    "STEP":        "Step",
    "DECISION":    "Decision",
    "ANNOTATION":  "Annotation",
    "GROUP_LIMIT": "GroupLimit",
    "CODE":        "Code",
    "DATE_COND":   "DateCondition",
    "REFERENCE":   "Reference",
}


def _flatten_details(d: dict) -> dict:
    """Neo4j properties cannot be nested. Coerce list/dict values into strings."""
    out = {}
    for k, v in (d or {}).items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            try:
                out[k] = [str(x) for x in v if x is not None]
            except Exception:
                out[k] = str(v)
        elif isinstance(v, dict):
            import json as _json
            out[k] = _json.dumps(v)[:1000]
        elif isinstance(v, (bool, int, float, str)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def neo4j_graph_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Materialise the SOP knowledge graph in Neo4j.

    Source of truth (in priority order):
      1. state["audit_graph_nodes"] + state["audit_graph_edges"]
         (produced by the agentic graph_synthesis_stage — LLM-built)
      2. uhc_sop_ingestion.graph_builder.build_audit_graph(state)
         (deterministic fallback)
    """
    sop_id = state.get("neo4j_sop_id", "")
    if not sop_id:
        logger.warning("neo4j_graph_writer: missing neo4j_sop_id — skipping")
        return {}

    nodes = state.get("audit_graph_nodes") or []
    edges = state.get("audit_graph_edges") or []
    source = "agentic_llm"
    if not nodes:
        from uhc_sop_ingestion.graph_builder import build_audit_graph
        nodes, edges = build_audit_graph(state)
        source = "deterministic_fallback"
    if not nodes:
        return {}

    try:
        drv = _driver(cfg)
    except Exception as e:
        logger.warning("neo4j_graph_writer: driver unavailable — %s", e)
        return {}

    try:
        with drv.session(database=cfg.neo4j_database) as s:
            # Wipe stale graph nodes/edges for this SOP first.
            _run(s, """
                MATCH (n {sop_id:$sop_id})
                WHERE n.is_graph_node = true
                DETACH DELETE n
            """, {"sop_id": sop_id})

            # Insert nodes. Each node gets a Label corresponding to its type
            # AND the marker label :GraphNode so we can later wipe in bulk.
            for n in nodes:
                label = _TYPE_LABEL.get(n["type"], "GraphNode")
                props = _flatten_details(n.get("details", {}))
                props.update({
                    "sop_id":    sop_id,
                    "node_key":  n["key"],
                    "node_type": n["type"],
                    "label":     n["label"],
                    "display_order": int(n.get("order", 0) or 0),
                    "is_graph_node": True,
                })
                _run(s,
                    f"""
                    MERGE (x:{label}:GraphNode {{sop_id:$sop_id, node_key:$node_key}})
                    SET x += $props, x.updated_at = datetime()
                    """,
                    {"sop_id": sop_id, "node_key": n["key"], "props": props},
                )

            # Insert edges with relationship-type set dynamically via apoc-style
            # CALL — but Neo4j drivers don't allow dynamic rel-types via params,
            # so we issue one query per rel-type. Group edges by rel for batching.
            from collections import defaultdict
            by_rel: dict[str, list[dict]] = defaultdict(list)
            for e in edges:
                by_rel[e["rel"]].append(e)

            for rel, e_list in by_rel.items():
                _run(s,
                    f"""
                    UNWIND $edges AS edge
                    MATCH (src:GraphNode {{sop_id:$sop_id, node_key:edge.source}})
                    MATCH (tgt:GraphNode {{sop_id:$sop_id, node_key:edge.target}})
                    MERGE (src)-[r:{rel}]->(tgt)
                    SET r.label = edge.label, r.details = edge.details_str
                    """,
                    {
                        "sop_id": sop_id,
                        "edges": [
                            {
                                "source": e["source"],
                                "target": e["target"],
                                "label":  e.get("label", ""),
                                "details_str": __import__("json").dumps(
                                    e.get("details", {}))[:500],
                            }
                            for e in e_list
                        ],
                    },
                )
    except Exception as e:
        logger.error("neo4j_graph_writer: %s", e)
        return {"errors": [{"agent": "neo4j_graph_writer", "msg": str(e)}]}

    logger.info("neo4j_graph_writer[%s]: wrote %d nodes, %d edges for %s",
                source, len(nodes), len(edges), sop_id)
    return {"neo4j_graph_nodes": len(nodes),
            "neo4j_graph_edges": len(edges),
            "audit_graph_source": source}
