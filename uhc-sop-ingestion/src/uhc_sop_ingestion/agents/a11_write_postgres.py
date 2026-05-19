"""
a11_write_postgres.py — Claims Audit PostgreSQL Write Layer

Writes parsed SOP content to the claims-audit-oriented schema:

  sop_ingestion_auditsop             — root SOP document record
  sop_ingestion_auditprecondition    — pre-conditions the auditor checks first
  sop_ingestion_auditstep            — each decision point in the workflow
  sop_ingestion_auditdecision        — If/Then rows (the audit logic)
  sop_ingestion_auditgrouplimit      — group-specific timely filing limits
  sop_ingestion_auditcode            — every claims code mentioned
  sop_ingestion_auditdatecondition   — date-based applicability conditions
  sop_ingestion_auditannotation      — notes, alerts, exceptions
  sop_ingestion_auditreference       — cross-references to other SOPs

Every function follows the LangGraph agent signature: (state, cfg) -> dict | None.
Uses raw psycopg2 with autocommit so writes are immediately visible.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uhc_sop_ingestion.state import PipelineState
    from uhc_sop_ingestion.config import PipelineConfig

log = logging.getLogger(__name__)

# ── psycopg2 connection helper ────────────────────────────────────────────────

def _conn(cfg: "PipelineConfig"):
    import psycopg2
    c = psycopg2.connect(cfg.pg_dsn)
    c.autocommit = True
    return c


def _exec(cfg: "PipelineConfig", sql: str, params: tuple) -> list:
    """Execute SQL, return fetchall() rows. Logs and swallows errors."""
    try:
        c = _conn(cfg)
        with c:
            with c.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
        c.close()
        return rows
    except Exception as exc:
        log.warning("pg write error: %s", exc)
        return []

# ── serialization helpers ─────────────────────────────────────────────────────

def _strip_nul(obj: Any) -> Any:
    """Recursively strip NUL (\\x00) bytes from any str inside obj.

    Postgres (text + jsonb) rejects strings containing NUL bytes, which
    sometimes appear in LLM output or upstream-parsed HTML.
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "") if "\x00" in obj else obj
    if isinstance(obj, dict):
        return {k: _strip_nul(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_nul(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_strip_nul(x) for x in obj)
    return obj


def _j(v: Any) -> str:
    return json.dumps(_strip_nul(v) if v is not None else [])


def _s(v: Any, maxlen: int | None = None) -> str:
    s = str(v) if v is not None else ""
    if "\x00" in s:
        s = s.replace("\x00", "")
    return s[:maxlen] if maxlen else s


def _i(v: Any):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ── code classification ────────────────────────────────────────────────────────

def _classify_code(code: str) -> str:
    """Pattern-based code type classifier used as a fallback when the LLM hasn't classified."""
    c = code.strip().upper()
    if re.match(r'^[EFW]\d{2}$', c):             return "EOB"
    if re.match(r'^\d{3}$', c):                   return "EX"
    if c in {"CDD", "CDS", "CDA"}:                return "DENIAL"
    if re.match(r'^F[3-9]$', c):                  return "SYSTEM_ACT"
    if re.match(r'^\d{2}$', c):                   return "POS"
    if re.match(r'^\d{4}$', c):                   return "REVENUE"
    if re.match(r'^[A-Z]\d{4}$|^\d{5}$', c):     return "CPT"
    return "UNKNOWN"


def _classify_decision(action_text: str, denial: list, eob: list) -> str:
    t = (action_text or "").upper()
    if denial or "CDD" in t:          return "DENY"
    if "DENY" in t or "DENIAL" in t:  return "DENY"
    if "BYPASS" in t or "OVERRIDE" in t: return "BYPASS"
    if "PEND" in t:                   return "PEND"
    if "ALLOW" in t or ("PROCESS" in t and "F3" in t): return "ALLOW"
    if "WAIVE" in t:                  return "WAIVE"
    if "STOP" in t or "DO NOT" in t:  return "STOP"
    return "CONDITIONAL"


def _merge_codes(*lists) -> list:
    seen, result = set(), []
    for lst in lists:
        for c in (lst or []):
            if c not in seen:
                seen.add(c); result.append(c)
    return result

# ── write agents ───────────────────────────────────────────────────────────────

def pg_sop_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """
    Insert/upsert the AuditSop row.
    Returns {"sop_db_id": <int>} so all child writers can reference it.
    A human claims auditor opens this record first.
    """
    meta     = state.get("metadata") or {}
    steps    = state.get("steps") or []
    pre_secs = state.get("pre_sections") or []
    codes    = state.get("detected_codes") or []
    dec_cnt  = sum(len(s.get("decision_rows") or s.get("rows") or []) for s in steps)

    sql = """
        INSERT INTO sop_ingestion_auditsop (
            job_id, url, content_hash, doc_format, neo4j_sop_id,
            title, purpose, llm_summary, narrative_context,
            platform, lob, audience, state_div, product,
            effective_date, revision_date,
            crawl_depth, parent_url,
            step_count, decision_count, code_count, precondition_count,
            raw_text, parse_warnings, crawled_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s::jsonb, %s::jsonb, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, %s, %s,
            %s, %s::jsonb, NOW(), NOW()
        )
        ON CONFLICT ON CONSTRAINT unique_auditsop_job_hash
        DO UPDATE SET
            title              = EXCLUDED.title,
            purpose            = EXCLUDED.purpose,
            llm_summary        = EXCLUDED.llm_summary,
            narrative_context  = CASE
                WHEN EXCLUDED.narrative_context <> ''
                THEN EXCLUDED.narrative_context
                ELSE sop_ingestion_auditsop.narrative_context
            END,
            step_count         = EXCLUDED.step_count,
            decision_count     = EXCLUDED.decision_count,
            code_count         = EXCLUDED.code_count,
            precondition_count = EXCLUDED.precondition_count,
            raw_text           = EXCLUDED.raw_text,
            updated_at         = NOW()
        RETURNING id;
    """
    rows = _exec(cfg, sql, (
        state.get("job_id", ""),
        _s(state.get("current_url", ""), 2048),
        _s(state.get("content_hash", "x"), 64),
        _s(meta.get("doc_format", "HTML"), 8),
        _s(meta.get("sop_id", ""), 256),
        _s(meta.get("title", ""), 4096),
        _s(meta.get("purpose", ""), 4096),
        _s(state.get("llm_summary", meta.get("llm_summary", "")), 8192),
        _s(state.get("sop_narrative", "")),
        _s(meta.get("platform", ""), 256),
        _j(meta.get("lob", [])),
        _j(meta.get("audience", [])),
        _s(meta.get("state_div", ""), 256),
        _s(meta.get("product", ""), 256),
        _s(meta.get("effective_date", ""), 32),
        _s(meta.get("revision_date", ""), 32),
        _i(state.get("current_depth", 0)) or 0,
        _s(state.get("current_parent_url", ""), 2048),
        len(steps),
        dec_cnt,
        len(codes),
        len(pre_secs),
        _s(state.get("raw_text", ""))[:500_000],
        _j(state.get("parse_warnings", [])),
    ))

    if rows:
        sop_id = rows[0][0]
        log.info("pg_sop_writer: sop_db_id=%s", sop_id)
        return {"sop_db_id": sop_id}
    return {}


def pg_precondition_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """
    Write AuditPrecondition rows AND promote exception-block rules to a
    'Step 0 — Pre-Step Exceptions' AuditStep with proper AuditDecision rows.

    Rule: if a pre-section has llm_rules with is_exception=True or decision_type
    DENY/ALLOW/BYPASS/OVERRIDE, those rules become decision rows in Step 0 so
    auditors see them in the decision tree (not just buried in metadata).
    """
    sop_id = state.get("sop_db_id")
    if not sop_id:
        return {}

    CATEGORY_MAP = {
        "platform": "PLATFORM", "audience": "AUDIENCE",
        "line": "LOB", "lob": "LOB", "business": "LOB",
        "eligib": "ELIGIBILITY", "coverage": "COVERAGE",
        "exception": "EXCEPTION", "exclusion": "EXCEPTION", "override": "EXCEPTION",
    }

    def _cat(label: str) -> str:
        lo = label.lower()
        for kw, cat in CATEGORY_MAP.items():
            if kw in lo:
                return cat
        return "GENERAL"

    pc_sql = """
        INSERT INTO sop_ingestion_auditprecondition
            (sop_id, display_order, category, label, content_text, llm_rules, is_blocking)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT DO NOTHING;
    """

    # ── Collect all exception-type rules across pre-sections ─────────────────
    exception_decisions: list[dict] = []

    for idx, ps in enumerate(state.get("pre_sections") or []):
        name  = _s(ps.get("name", ""), 512)
        items = ps.get("items", [])
        content = "\n".join(
            (i.get("text", str(i)) if isinstance(i, dict) else str(i))
            for i in items
        )
        # Fix: the extractor writes to llm_rules, not rules/annotations
        llm_rules = ps.get("llm_rules") or ps.get("rules") or ps.get("annotations") or []
        _exec(cfg, pc_sql, (
            sop_id, idx, _cat(name), name, content, _j(llm_rules), False
        ))

        # Collect rules that belong in the decision tree
        section_label = name
        for rule in llm_rules:
            if not isinstance(rule, dict):
                continue
            dtype = (rule.get("decision_type") or "NOTE").upper()
            is_exc = rule.get("is_exception") or dtype in (
                "DENY", "ALLOW", "BYPASS", "OVERRIDE", "ELIGIBILITY"
            )
            if is_exc:
                exception_decisions.append({
                    "section":   section_label,
                    "condition": rule.get("condition", ""),
                    "action":    rule.get("action", ""),
                    "dtype":     dtype,
                })

    if not exception_decisions:
        return {}

    # ── Create Step 0 — Pre-Step Exceptions ──────────────────────────────────
    step0_sql = """
        INSERT INTO sop_ingestion_auditstep
            (sop_id, step_number, question, intro_text, is_terminal,
             terminal_action, is_sub_procedure, sub_procedure_name,
             neo4j_node_id)
        VALUES (%s, 0, %s, %s, false, '', false, '', '')
        ON CONFLICT (sop_id, step_number) DO UPDATE
          SET question = EXCLUDED.question, intro_text = EXCLUDED.intro_text
        RETURNING id;
    """
    rows = _exec(cfg, step0_sql, (
        sop_id,
        "Pre-Step Exceptions & Override Rules",
        "Check these exception conditions BEFORE entering Step 1. "
        "If any condition matches, the standard steps may not apply.",
    ))
    if not rows:
        return {}
    step0_id = rows[0][0]

    # Clear any old Step-0 decisions before re-writing (idempotent on re-ingest)
    _exec(cfg, "DELETE FROM sop_ingestion_auditdecision WHERE step_id = %s;",
          (step0_id,))

    # ── Write each exception rule as an AuditDecision ─────────────────────────
    dec_sql = """
        INSERT INTO sop_ingestion_auditdecision
            (step_id, row_index, condition_if, condition_and, action_text,
             action_summary, action_line, action_claim,
             decision_type, goto_step, is_final,
             eob_codes, ex_codes, denial_codes, system_actions, all_codes,
             neo4j_edge_id)
        VALUES (%s, %s, %s, '', %s, %s, '', '', %s, NULL, false,
                '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
                '');
    """
    # Map our LLM-tagged decision types to the schema's allowed CHOICES
    _DTYPE_MAP = {
        "DENY": "DENY", "ALLOW": "ALLOW", "BYPASS": "BYPASS",
        "PEND": "PEND", "REFER": "REFER", "SYSTEM": "SYSTEM",
        "STOP": "STOP", "WAIVE": "WAIVE", "OVERRIDE": "BYPASS",
        "ELIGIBILITY": "CONDITIONAL", "NOTE": "CONDITIONAL",
    }
    for i, rule in enumerate(exception_decisions):
        mapped = _DTYPE_MAP.get(rule["dtype"], "CONDITIONAL")
        _exec(cfg, dec_sql, (
            step0_id, i,
            _s(rule["condition"], 1000),
            _s(rule["action"], 2000),
            _s(f"[{rule['section']}] {rule['action']}", 500)[:500],
            mapped,
        ))

    # Update step_count on the AuditSop to include Step 0
    _exec(cfg, """
        UPDATE sop_ingestion_auditsop
        SET step_count = step_count + 1,
            decision_count = decision_count + %s
        WHERE id = %s
    """, (len(exception_decisions), sop_id))

    log.info("pg_precondition_writer: wrote Step 0 with %d exception rules",
             len(exception_decisions))
    return {}


def pg_step_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """
    Write AuditStep + AuditDecision rows.
    This is the heart of the claims audit trail:
      - Each step is a decision point the auditor visits
      - Each decision row is one If/Then rule the auditor evaluates
    """
    sop_id = state.get("sop_db_id")
    if not sop_id:
        return {}

    VALID_DECISIONS = {"DENY","ALLOW","BYPASS","PEND","REFER","SYSTEM","STOP","WAIVE","CONDITIONAL"}

    step_sql = """
        INSERT INTO sop_ingestion_auditstep
            (sop_id, step_number, question, intro_text,
             is_terminal, terminal_action,
             is_sub_procedure, sub_procedure_name, neo4j_node_id,
             narrative_context)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT ON CONSTRAINT unique_auditstep_sop_number
        DO UPDATE SET
            question          = EXCLUDED.question,
            intro_text        = EXCLUDED.intro_text,
            is_terminal       = EXCLUDED.is_terminal,
            terminal_action   = EXCLUDED.terminal_action,
            narrative_context = CASE
                WHEN EXCLUDED.narrative_context <> ''
                THEN EXCLUDED.narrative_context
                ELSE sop_ingestion_auditstep.narrative_context
            END
        RETURNING id;
    """

    dec_sql = """
        INSERT INTO sop_ingestion_auditdecision
            (step_id, row_index,
             condition_if, condition_and, action_text,
             action_summary, action_line, action_claim,
             decision_type, goto_step, is_final,
             eob_codes, ex_codes, denial_codes, system_actions, all_codes,
             neo4j_edge_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s)
        ON CONFLICT DO NOTHING;
    """

    for step in (state.get("steps") or []):
        step_num = _i(step.get("step_number") or step.get("number"))
        if step_num is None:
            continue

        rows = _exec(cfg, step_sql, (
            sop_id, step_num,
            _s(step.get("question", step.get("title", ""))),
            _s(step.get("intro_text", step.get("note", ""))),
            bool(step.get("is_terminal", False)),
            _s(step.get("terminal_action", step.get("action", "")), 64),
            bool(step.get("is_sub_procedure", False)),
            _s(step.get("sub_procedure_name", ""), 256),
            "",
            _s(step.get("narrative_context", "")),
        ))
        if not rows:
            continue
        step_db_id = rows[0][0]

        # Parser uses "decision_rows" key; fallback to "rows" for compatibility
        decision_rows = step.get("decision_rows") or step.get("rows") or []
        for ridx, row in enumerate(decision_rows):
            eob     = row.get("eob_codes") or []
            ex      = row.get("ex_codes") or []
            denial  = row.get("denial_codes") or []
            sys_act = row.get("system_actions") or []

            # Fallback: classify codes from flat "codes" list
            if not (eob or ex or denial or sys_act):
                for c in (row.get("codes") or []):
                    ct = _classify_code(c)
                    if ct == "EOB":          eob.append(c)
                    elif ct == "EX":         ex.append(c)
                    elif ct == "DENIAL":     denial.append(c)
                    elif ct == "SYSTEM_ACT": sys_act.append(c)

            all_c  = _merge_codes(eob, ex, denial, sys_act)
            dtype  = _s(row.get("decision") or row.get("decision_type") or "", 16).upper()
            if dtype not in VALID_DECISIONS:
                action = _s(row.get("action") or row.get("then") or "")
                dtype  = _classify_decision(action, denial, eob)

            _exec(cfg, dec_sql, (
                step_db_id, ridx,
                _s(row.get("condition_if", row.get("if", ""))),
                _s(row.get("condition_and", row.get("and", ""))),
                _s(row.get("action", row.get("then", ""))),
                _s(row.get("action_summary", "")),
                _s(row.get("action_line", "")),
                _s(row.get("action_claim", "")),
                dtype,
                _i(row.get("skip_to_step") or row.get("goto_step")),
                bool(row.get("is_terminal") or row.get("is_final", False)),
                _j(eob), _j(ex), _j(denial), _j(sys_act), _j(all_c),
                "",
            ))
    return {}


def pg_group_limit_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """
    Write AuditGroupLimit rows.
    The auditor looks up the claim's group here to determine
    how many days the provider had to submit.
    """
    sop_id = state.get("sop_db_id")
    if not sop_id:
        return {}

    sql = """
        INSERT INTO sop_ingestion_auditgrouplimit
            (sop_id, group_name, inn_days, oon_days, limit_days,
             limit_months, limit_years, calculation_basis, network_type,
             member_submitted_only, exceptions, special_notes, raw_text)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s)
        ON CONFLICT DO NOTHING;
    """
    for gr in (state.get("group_rules") or []):
        calc = _s(gr.get("calculation_from") or gr.get("calculation_basis") or "DOS", 32).upper()
        if calc not in {"DOS", "PAID_DATE", "EOB_DATE"}:
            calc = "DOS"
        _exec(cfg, sql, (
            sop_id,
            _s(gr.get("group_name") or gr.get("name") or "UNKNOWN", 256),
            _i(gr.get("inn_days")),
            _i(gr.get("oon_days")),
            _i(gr.get("limit_days")),
            _i(gr.get("limit_months")),
            _i(gr.get("limit_years")),
            calc,
            _s(gr.get("network_type", "BOTH"), 8),
            bool(gr.get("member_submitted_only", False)),
            _j(gr.get("exceptions") or []),
            _j(gr.get("special_notes") or []),
            _s(gr.get("raw_text", "")),
        ))
    return {}


def pg_code_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """
    Write AuditCode rows.
    Every claims code (EOB, EX, denial, system action) mentioned in the SOP.
    The auditor uses this as a reference to know exactly which code to apply.
    """
    sop_id = state.get("sop_db_id")
    if not sop_id:
        return {}

    VALID_TYPES = {"EOB","EX","DENIAL","SYSTEM_ACT","POS","REVENUE",
                   "BILL_TYPE","MODIFIER","FREQUENCY","CPT","UNKNOWN"}
    sql = """
        INSERT INTO sop_ingestion_auditcode
            (sop_id, code_value, code_type, description,
             context_snippet, source_step, source_field, confidence)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT ON CONSTRAINT unique_auditcode DO NOTHING;
    """
    for c in (state.get("detected_codes") or []):
        val = _s(c.get("raw_value") or c.get("code") or c.get("value") or "", 64)
        if not val:
            continue
        ctype = _s(c.get("code_system") or c.get("code_type") or "", 16).upper()
        if ctype not in VALID_TYPES:
            ctype = _classify_code(val)
        _exec(cfg, sql, (
            sop_id, val, ctype,
            _s(c.get("description", "")),
            _s(c.get("context_snippet") or c.get("context") or ""),
            _i(c.get("source_step")),
            _s(c.get("source_field", ""), 256),
            float(c.get("confidence", 1.0)),
        ))
    return {}


def pg_date_condition_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Write AuditDateCondition rows — date-range conditions that affect rule applicability."""
    sop_id = state.get("sop_db_id")
    if not sop_id:
        return {}

    sql = """
        INSERT INTO sop_ingestion_auditdatecondition
            (sop_id, date_from, date_to, effective_date, context_text, applies_to)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING;
    """
    for dc in (state.get("detected_date_conditions") or []):
        _exec(cfg, sql, (
            sop_id,
            _s(dc.get("date_from", ""), 32),
            _s(dc.get("date_to", ""), 32),
            _s(dc.get("effective_date", ""), 32),
            _s(dc.get("context") or dc.get("context_text") or ""),
            _s(dc.get("source_field") or dc.get("applies_to") or "", 256),
        ))
    return {}


def pg_annotation_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """
    Write AuditAnnotation rows — notes, alerts, exceptions.
    The auditor MUST read these before making a final claim decision.
    """
    sop_id = state.get("sop_db_id")
    if not sop_id:
        return {}

    VALID = {"NOTE","ALERT","EXCEPTION","TIP","WARNING","HIGHLIGHT"}
    sql = """
        INSERT INTO sop_ingestion_auditannotation
            (sop_id, step_id, annotation_type, content_text, is_claim_impact)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING;
    """
    IMPACT_KEYWORDS = {"DENY","BYPASS","CODE","PEND","OVERRIDE","E51","F51","346"}

    for ann in (state.get("annotations") or []):
        atype = _s(ann.get("type") or ann.get("annotation_type") or "NOTE", 16).upper()
        if atype not in VALID:
            atype = "NOTE"
        content = _s(ann.get("text") or ann.get("content") or ann.get("content_text") or "")
        if not content:
            continue
        impact = bool(ann.get("is_claim_impact") or
                      any(k in content.upper() for k in IMPACT_KEYWORDS))
        _exec(cfg, sql, (sop_id, None, atype, content, impact))
    return {}


def pg_reference_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Write AuditReference rows — cross-references the auditor may need to consult."""
    sop_id = state.get("sop_db_id")
    if not sop_id:
        return {}

    sql = """
        INSERT INTO sop_ingestion_auditreference
            (sop_id, step_id, ref_text, ref_url, ref_type, is_resolved)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING;
    """
    for lnk in (state.get("links") or []):
        href     = _s(lnk.get("href") or lnk.get("url") or "")
        text     = _s(lnk.get("link_text") or lnk.get("text") or "")
        ltype    = _s(lnk.get("link_type") or "UNRESOLVED", 32)
        resolved = bool(lnk.get("resolved_url") or lnk.get("status") == "OK")
        _exec(cfg, sql, (sop_id, None, text, href, ltype, resolved))
    return {}


def pg_job_updater(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Increment the IngestionJob's docs_processed counter."""
    sql = """
        UPDATE sop_ingestion_ingestionjob
        SET docs_processed = docs_processed + 1,
            updated_at     = NOW()
        WHERE job_id = %s;
    """
    _exec(cfg, sql, (state.get("job_id", ""),))
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# pg_graph_writer  — materialise the SOP knowledge graph as nodes & edges
# ─────────────────────────────────────────────────────────────────────────────

def pg_graph_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Persist the canonical SOP knowledge graph into AuditGraphNode/AuditGraphEdge.

    Source of truth (in priority order):
      1. state["audit_graph_nodes"] + state["audit_graph_edges"]
         (produced by the agentic graph_synthesis_stage — LLM-built)
      2. uhc_sop_ingestion.graph_builder.build_audit_graph(state)
         (deterministic fallback so PG/Neo4j are NEVER empty)

    Re-runnable: wipes existing graph rows for this SOP first.
    """
    sop_id = state.get("sop_db_id")
    if not sop_id:
        log.warning("pg_graph_writer: no sop_db_id in state — skipping")
        return {}

    nodes = state.get("audit_graph_nodes") or []
    edges = state.get("audit_graph_edges") or []
    source = "agentic_llm"
    if not nodes:
        from uhc_sop_ingestion.graph_builder import build_audit_graph
        nodes, edges = build_audit_graph(state)
        source = "deterministic_fallback"
        log.info("pg_graph_writer: no agentic graph in state — used deterministic builder")

    if not nodes:
        log.info("pg_graph_writer: no nodes from any source — skipping")
        return {}

    from collections import Counter as _Counter
    type_counts = _Counter(n.get("type", "?") for n in nodes)
    log.info("pg_graph_writer[%s]: producing %s", source, dict(type_counts))

    # Clear any previous graph rows for this SOP so re-ingest is clean.
    _exec(cfg, """
        DELETE FROM sop_ingestion_auditgraphedge WHERE sop_id = %s;
    """, (sop_id,))
    _exec(cfg, """
        DELETE FROM sop_ingestion_auditgraphnode WHERE sop_id = %s;
    """, (sop_id,))

    # Insert nodes; build a node_key → primary_key map for edges.
    key_to_id: dict[str, int] = {}
    node_sql = """
        INSERT INTO sop_ingestion_auditgraphnode
            (sop_id, node_key, node_type, label, details,
             ref_table, ref_id, display_order)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
        RETURNING id;
    """
    for n in nodes:
        rows = _exec(cfg, node_sql, (
            sop_id,
            _s(n["key"], 128),
            _s(n["type"], 24),
            _s(n["label"], 255),
            _j(n.get("details", {})),
            _s(n.get("ref_table", ""), 64),
            _i(n.get("ref_id")),
            int(n.get("order", 0) or 0),
        ))
        if rows:
            key_to_id[n["key"]] = rows[0][0]

    # Insert edges using the key→id map.
    edge_sql = """
        INSERT INTO sop_ingestion_auditgraphedge
            (sop_id, source_id, target_id, rel_type, label, details)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb);
    """
    skipped = 0
    for e in edges:
        src = key_to_id.get(e["source"])
        tgt = key_to_id.get(e["target"])
        if not (src and tgt):
            skipped += 1
            continue
        _exec(cfg, edge_sql, (
            sop_id, src, tgt,
            _s(e["rel"], 24), _s(e.get("label", ""), 255),
            _j(e.get("details", {})),
        ))

    log.info("pg_graph_writer[%s]: wrote %d nodes, %d edges (skipped %d) for sop %s",
             source, len(nodes), len(edges) - skipped, skipped, sop_id)
    # NOTE: do NOT overwrite state["audit_graph_nodes/edges"] (those are the
    # actual node/edge lists from the synthesis stage). Return persistence
    # stats under distinct keys for any downstream consumers.
    return {
        "audit_graph_persisted_nodes": len(nodes),
        "audit_graph_persisted_edges": len(edges) - skipped,
        "audit_graph_source": source,
    }
