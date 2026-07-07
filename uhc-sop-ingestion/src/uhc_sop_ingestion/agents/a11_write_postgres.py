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
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uhc_sop_ingestion.state import PipelineState
    from uhc_sop_ingestion.config import PipelineConfig

log = logging.getLogger(__name__)

# Tiny in-process cache so we don't hit information_schema on every document.
_COLUMN_EXISTS_CACHE: dict[tuple[str, str], bool] = {}


def _ir_persist_enabled() -> bool:
    """When SOP_IR_PERSIST is on, the canonical-IR gate (sop_ir.persist.persist_ir,
    invoked post-run from pipeline_runner) is the AUTHORITATIVE writer for
    AuditStep/AuditDecision. The flat writer below then skips its step/decision
    pass so the two never fight over the same rows.

    Default ON: the nested-routing IR is the seamless high-fidelity path. An
    operator opts out only by setting SOP_IR_PERSIST to a falsy value
    (0/false/no/off). Must stay in lockstep with
    sop_ingestion.pipeline_runner._ir_persist_enabled."""
    raw = os.environ.get("SOP_IR_PERSIST")
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


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


def _table_has_column(cfg: "PipelineConfig", table_name: str, column_name: str) -> bool:
    """Best-effort schema probe used by raw SQL writers.

    We support mixed environments where DB schema may be ahead/behind package
    code. If the probe fails, assume column does not exist and keep writes safe.
    """
    key = (table_name, column_name)
    cached = _COLUMN_EXISTS_CACHE.get(key)
    if cached is not None:
        return cached

    rows = _exec(
        cfg,
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
              AND column_name = %s
        );
        """,
        (table_name, column_name),
    )
    exists = bool(rows and rows[0] and rows[0][0])
    _COLUMN_EXISTS_CACHE[key] = exists
    return exists


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
    if re.match(r"^[EFW]\d{2}$", c):
        return "EOB"
    if re.match(r"^\d{3}$", c):
        return "EX"
    if c in {"CDD", "CDS", "CDA"}:
        return "DENIAL"
    if re.match(r"^F[3-9]$", c):
        return "SYSTEM_ACT"
    if re.match(r"^\d{2}$", c):
        return "POS"
    if re.match(r"^\d{4}$", c):
        return "REVENUE"
    if re.match(r"^[A-Z]\d{4}$|^\d{5}$", c):
        return "CPT"
    return "UNKNOWN"


def _classify_decision(action_text: str, denial: list, eob: list) -> str:
    t = (action_text or "").upper()
    if denial or "CDD" in t:
        return "DENY"
    if "DENY" in t or "DENIAL" in t:
        return "DENY"
    if "BYPASS" in t or "OVERRIDE" in t:
        return "BYPASS"
    if "PEND" in t:
        return "PEND"
    if "ALLOW" in t or ("PROCESS" in t and "F3" in t):
        return "ALLOW"
    if "WAIVE" in t:
        return "WAIVE"
    if "STOP" in t or "DO NOT" in t:
        return "STOP"
    return "CONDITIONAL"


def _merge_codes(*lists) -> list:
    seen, result = set(), []
    for lst in lists:
        for c in lst or []:
            if c not in seen:
                seen.add(c)
                result.append(c)
    return result


# ── write agents ───────────────────────────────────────────────────────────────


def pg_sop_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """
    Insert/upsert the AuditSop row.
    Returns {"sop_db_id": <int>} so all child writers can reference it.
    A human claims auditor opens this record first.
    """
    from uhc_sop_ingestion.revision import normalize_canonical_url

    meta = state.get("metadata") or {}
    steps = state.get("steps") or []
    pre_secs = state.get("pre_sections") or []
    codes = state.get("detected_codes") or []
    dec_cnt = sum(len(s.get("decision_rows") or s.get("rows") or []) for s in steps)

    current_url = _s(state.get("current_url", ""), 2048)
    canonical_url = _s(
        state.get("canonical_url")
        or meta.get("canonical_url")
        or normalize_canonical_url(current_url),
        2048,
    )
    include_canonical_url = _table_has_column(
        cfg, "sop_ingestion_auditsop", "canonical_url"
    )
    include_version_number = _table_has_column(
        cfg, "sop_ingestion_auditsop", "version_number"
    )
    include_is_current = _table_has_column(
        cfg, "sop_ingestion_auditsop", "is_current"
    )
    include_version_action = _table_has_column(
        cfg, "sop_ingestion_auditsop", "version_action"
    )
    include_activation_status = _table_has_column(
        cfg, "sop_ingestion_auditsop", "activation_status"
    )
    include_supersedes_id = _table_has_column(
        cfg, "sop_ingestion_auditsop", "supersedes_id"
    )
    requires_review = bool(state.get("requires_human_review"))
    if state.get("version_action"):
        version_action = _s(state.get("version_action", "NEW"), 64)
        is_current = not requires_review
        activation_status = "pending_review" if requires_review else "active"
    else:
        is_current = bool(meta.get("is_current", True))
        version_action = _s(meta.get("version_action", "NEW"), 64)
        activation_status = _s(meta.get("activation_status", "active"), 64)
    version_number = _i(meta.get("version_number")) or 1
    prior_sop_id = _i(state.get("prior_sop_db_id"))

    columns = [
        "job_id", "url", "content_hash", "doc_format", "neo4j_sop_id",
        "title", "purpose", "llm_summary", "narrative_context",
        "platform", "lob", "audience", "state_div", "product",
        "effective_date", "revision_date",
        "crawl_depth", "parent_url",
        "step_count", "decision_count", "code_count", "precondition_count",
        "raw_text", "parse_warnings", "crawled_at", "updated_at",
    ]
    placeholders = [
        "%s", "%s", "%s", "%s", "%s",
        "%s", "%s", "%s", "%s",
        "%s", "%s::jsonb", "%s::jsonb", "%s", "%s",
        "%s", "%s",
        "%s", "%s",
        "%s", "%s", "%s", "%s",
        "%s", "%s::jsonb", "NOW()", "NOW()",
    ]
    params: list[Any] = [
        state.get("job_id", ""),
        current_url,
        _s(state.get("content_hash", "x"), 64),
        _s(state.get("doc_format") or meta.get("doc_format", "HTML"), 8),
        _s(state.get("neo4j_sop_id") or meta.get("sop_id", ""), 256),
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
    ]

    if include_canonical_url:
        columns.insert(2, "canonical_url")
        placeholders.insert(2, "%s")
        params.insert(2, canonical_url)
    if include_version_number:
        columns.append("version_number")
        placeholders.append("%s")
        params.append(version_number)
    if include_is_current:
        columns.append("is_current")
        placeholders.append("%s")
        params.append(is_current)
    if include_version_action:
        columns.append("version_action")
        placeholders.append("%s")
        params.append(version_action)
    if include_activation_status:
        columns.append("activation_status")
        placeholders.append("%s")
        params.append(activation_status)
    if include_supersedes_id and prior_sop_id:
        columns.append("supersedes_id")
        placeholders.append("%s")
        params.append(prior_sop_id)

    update_set = [
        "title              = EXCLUDED.title",
        "purpose            = EXCLUDED.purpose",
        "llm_summary        = EXCLUDED.llm_summary",
        """narrative_context  = CASE
                WHEN EXCLUDED.narrative_context <> ''
                THEN EXCLUDED.narrative_context
                ELSE sop_ingestion_auditsop.narrative_context
            END""",
        "effective_date     = EXCLUDED.effective_date",
        "revision_date      = EXCLUDED.revision_date",
        "platform           = EXCLUDED.platform",
        "lob                = EXCLUDED.lob",
        "audience           = EXCLUDED.audience",
        "step_count         = EXCLUDED.step_count",
        "decision_count     = EXCLUDED.decision_count",
        "code_count         = EXCLUDED.code_count",
        "precondition_count = EXCLUDED.precondition_count",
        "raw_text           = EXCLUDED.raw_text",
        "updated_at         = NOW()",
    ]
    if include_canonical_url:
        update_set.insert(0, "canonical_url      = EXCLUDED.canonical_url")
    if include_version_number:
        update_set.insert(0, "version_number     = EXCLUDED.version_number")
    if include_is_current:
        update_set.insert(0, "is_current         = EXCLUDED.is_current")
    if include_version_action:
        update_set.insert(0, "version_action     = EXCLUDED.version_action")
    if include_activation_status:
        update_set.insert(0, "activation_status  = EXCLUDED.activation_status")

    sql = f"""
        INSERT INTO sop_ingestion_auditsop (
            {", ".join(columns)}
        ) VALUES (
            {", ".join(placeholders)}
        )
        ON CONFLICT ON CONSTRAINT unique_auditsop_job_hash
        DO UPDATE SET
            {",\n            ".join(update_set)}
        RETURNING id;
    """
    rows = _exec(cfg, sql, tuple(params))


    if rows:
        sop_id = rows[0][0]
        log.info("pg_sop_writer: sop_db_id=%s", sop_id)
        # ── Single-current invariant ─────────────────────────────────────────
        # The unique constraint is on (job, content_hash), so every re-ingest
        # is a NEW job → a NEW row inserted with is_current=TRUE. Without this
        # step the table accumulates many is_current rows for one URL (seen in
        # prod: 14 current rows for one SOP), which breaks the version lookup
        # and leaves auto-build unable to pick a single SOP. The version
        # registry only supersedes by canonical_url/document and is skipped
        # entirely on UNCHANGED, so enforce the invariant HERE where it always
        # runs: demote every OTHER current row for the same URL.
        if include_is_current and is_current:
            set_clause = "is_current = FALSE"
            if include_activation_status:
                set_clause += ", activation_status = 'superseded'"
            demoted = _exec(cfg, f"""
                UPDATE sop_ingestion_auditsop
                   SET {set_clause}
                 WHERE id <> %s
                   AND is_current = TRUE
                   AND (
                       lower(rtrim(url, '/')) = lower(rtrim(%s, '/'))
                       OR (canonical_url <> '' AND canonical_url = %s)
                   )
             RETURNING id;
            """, (sop_id, current_url, canonical_url))
            if demoted:
                log.info(
                    "pg_sop_writer: superseded %d prior current AuditSop row(s) "
                    "for url=%s (ids=%s)",
                    len(demoted), current_url, [r[0] for r in demoted],
                )
        return {"sop_db_id": sop_id}
    log.warning(
        "pg_sop_writer: insert failed for job=%s url=%s — downstream PG writers will skip",
        state.get("job_id", ""),
        state.get("current_url", ""),
    )
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
        "platform": "PLATFORM",
        "audience": "AUDIENCE",
        "line": "LOB",
        "lob": "LOB",
        "business": "LOB",
        "eligib": "ELIGIBILITY",
        "coverage": "COVERAGE",
        "exception": "EXCEPTION",
        "exclusion": "EXCEPTION",
        "override": "EXCEPTION",
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

    # Rules already attached to their host step (pdf_exception_attacher) must NOT
    # be re-emitted as a standalone "Step 0" node — that is exactly the split we
    # want to avoid. Step 0 is now only built for exception rules with no
    # confident host step (e.g. other document layouts), so nothing is dropped.
    def _norm(s: str) -> str:
        return " ".join((s or "").lower().split())

    attached_sigs = {
        _norm(s) for s in (state.get("exception_rules_attached") or [])
    }

    # ── Collect all exception-type rules across pre-sections ─────────────────
    exception_decisions: list[dict] = []

    for idx, ps in enumerate(state.get("pre_sections") or []):
        name = _s(ps.get("name", ""), 512)
        items = ps.get("items", [])
        content = "\n".join(
            (i.get("text", str(i)) if isinstance(i, dict) else str(i)) for i in items
        )
        # Fix: the extractor writes to llm_rules, not rules/annotations
        llm_rules = (
            ps.get("llm_rules") or ps.get("rules") or ps.get("annotations") or []
        )
        _exec(
            cfg, pc_sql, (sop_id, idx, _cat(name), name, content, _j(llm_rules), False)
        )

        # Collect rules that belong in the decision tree
        section_label = name
        for rule in llm_rules:
            if not isinstance(rule, dict):
                continue
            dtype = (rule.get("decision_type") or "NOTE").upper()
            is_exc = rule.get("is_exception") or dtype in (
                "DENY",
                "ALLOW",
                "BYPASS",
                "OVERRIDE",
                "ELIGIBILITY",
            )
            if is_exc and _norm(rule.get("condition", "")) in attached_sigs:
                continue
            if is_exc:
                subs = [
                    {
                        "condition": _s(sr.get("condition", ""), 1000),
                        "action": _s(sr.get("action", ""), 2000),
                    }
                    for sr in (rule.get("sub_rules") or [])
                    if isinstance(sr, dict)
                    and (sr.get("condition") or sr.get("action"))
                ]
                exception_decisions.append(
                    {
                        "section": section_label,
                        "condition": rule.get("condition", ""),
                        "action": rule.get("action", ""),
                        "dtype": dtype,
                        "sub_rules": subs,
                    }
                )

    if not exception_decisions:
        return {}

    # De-duplicate: overlapping perception bands frequently yield the SAME
    # exception rule 2-3 times, each PARAPHRASED differently by the LLM, so an
    # exact (condition, action) key can't collapse them. dedupe_exception_rules
    # clusters identifier-list gates by their TIN/NPI set (globally unique per
    # provider group) and unions their sub-rules, and exact-dedups plain rules —
    # so Step 0 shows each override (and each provider) exactly once.
    from .a06e_pdf_synthesis import dedupe_exception_rules

    exception_decisions = dedupe_exception_rules(exception_decisions)

    # ── Create Step 0 — Pre-Step Exceptions ──────────────────────────────────
    step0_sql = """
        INSERT INTO sop_ingestion_auditstep
            (sop_id, step_number, question, intro_text, is_terminal,
             terminal_action, is_sub_procedure, sub_procedure_name,
             neo4j_node_id, narrative_context, is_out_of_scope, yaml_rule_id)
        VALUES (%s, 0, %s, %s, false, '', false, '', '', '', false, '')
        ON CONFLICT (sop_id, step_number) DO UPDATE
          SET question = EXCLUDED.question, intro_text = EXCLUDED.intro_text
        RETURNING id;
    """
    rows = _exec(
        cfg,
        step0_sql,
        (
            sop_id,
            "Pre-Step Exceptions & Override Rules",
            "Check these exception conditions BEFORE entering Step 1. "
            "If any condition matches, the standard steps may not apply.",
        ),
    )
    if not rows:
        return {}
    step0_id = rows[0][0]

    # Clear any old Step-0 decisions before re-writing (idempotent on re-ingest)
    _exec(
        cfg, "DELETE FROM sop_ingestion_auditdecision WHERE step_id = %s;", (step0_id,)
    )

    # ── Write each exception rule as an AuditDecision ─────────────────────────
    # Parent rows RETURN their id so an operative identifier list (e.g. the
    # excluded-TIN/Provider table) can be written as nested child decisions
    # (depth=1, parent_id set) — one per provider — instead of being flattened.
    dec_sql = """
        INSERT INTO sop_ingestion_auditdecision
            (step_id, parent_id, row_index, condition_if, condition_and, action_text,
             action_summary, action_line, action_claim,
             decision_type, goto_step, is_final,
             eob_codes, ex_codes, denial_codes, system_actions, all_codes,
             neo4j_edge_id,
             depth, subrule_id, table_name, aggregation, output_text,
             tooling_allowed, is_out_of_scope, mongo_subtree_ref,
             applicable_when)
        VALUES (%s, %s, %s, %s, '', %s, %s, '', '', %s, NULL, false,
                '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
                '',
                %s, %s, '', 'LEAF', '', true, false, '', '')
        RETURNING id;
    """
    # Map our LLM-tagged decision types to the schema's allowed CHOICES
    _DTYPE_MAP = {
        "DENY": "DENY",
        "ALLOW": "ALLOW",
        "BYPASS": "BYPASS",
        "PEND": "PEND",
        "REFER": "REFER",
        "SYSTEM": "SYSTEM",
        "STOP": "STOP",
        "WAIVE": "WAIVE",
        "OVERRIDE": "BYPASS",
        "ELIGIBILITY": "CONDITIONAL",
        "NOTE": "CONDITIONAL",
    }
    total_written = 0
    for i, rule in enumerate(exception_decisions):
        mapped = _DTYPE_MAP.get(rule["dtype"], "CONDITIONAL")
        prows = _exec(
            cfg,
            dec_sql,
            (
                step0_id,
                None,  # parent_id (top-level)
                i,
                _s(rule["condition"], 1000),
                _s(rule["action"], 2000),
                _s(f"[{rule['section']}] {rule['action']}", 500)[:500],
                mapped,
                0,  # depth
                "",  # subrule_id
            ),
        )
        total_written += 1
        parent_id = prows[0][0] if prows else None

        # Nested provider/identifier entries → child decisions under this rule.
        for j, sub in enumerate(rule.get("sub_rules") or []):
            if not parent_id:
                break
            _exec(
                cfg,
                dec_sql,
                (
                    step0_id,
                    parent_id,
                    j,
                    _s(sub.get("condition", ""), 1000),
                    _s(sub.get("action", ""), 2000),
                    _s(sub.get("action", ""), 500)[:500],
                    mapped,
                    1,  # depth
                    f"{i}.{j}",  # subrule_id
                ),
            )
            total_written += 1

    # Update step_count on the AuditSop to include Step 0
    _exec(
        cfg,
        """
        UPDATE sop_ingestion_auditsop
        SET step_count = step_count + 1,
            decision_count = decision_count + %s
        WHERE id = %s
    """,
        (total_written, sop_id),
    )

    log.info(
        "pg_precondition_writer: wrote Step 0 with %d exception rules (%d rows incl. sub-rules)",
        len(exception_decisions),
        total_written,
    )
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

    # When the canonical-IR persist gate owns step/decision writing, skip the
    # flat pass entirely — persist_ir (post-run, Django ORM) writes the same
    # rows with full routing fidelity (nesting, aggregation, goto, OOS).
    if _ir_persist_enabled():
        log.info(
            "pg_step_writer: SOP_IR_PERSIST on — deferring step/decision "
            "rows to sop_ir.persist.persist_ir"
        )
        return {}

    VALID_DECISIONS = {
        "DENY",
        "ALLOW",
        "BYPASS",
        "PEND",
        "REFER",
        "SYSTEM",
        "STOP",
        "WAIVE",
        "CONDITIONAL",
    }

    step_sql = """
        INSERT INTO sop_ingestion_auditstep
            (sop_id, step_number, question, intro_text,
             is_terminal, terminal_action,
             is_sub_procedure, sub_procedure_name, neo4j_node_id,
             narrative_context, is_out_of_scope, yaml_rule_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT ON CONSTRAINT unique_auditstep_sop_number
        DO UPDATE SET
            question          = EXCLUDED.question,
            intro_text        = EXCLUDED.intro_text,
            is_terminal       = EXCLUDED.is_terminal,
            terminal_action   = EXCLUDED.terminal_action,
            is_out_of_scope   = EXCLUDED.is_out_of_scope,
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
             neo4j_edge_id,
             depth, subrule_id, table_name, aggregation, output_text,
             tooling_allowed, is_out_of_scope, mongo_subtree_ref,
             applicable_when)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s,
                0, '', '', 'LEAF', '', true, %s, '', %s)
        ON CONFLICT DO NOTHING;
    """

    for step in state.get("steps") or []:
        step_num = _i(step.get("step_number") or step.get("number"))
        if step_num is None:
            continue

        rows = _exec(
            cfg,
            step_sql,
            (
                sop_id,
                step_num,
                _s(step.get("question", step.get("title", ""))),
                _s(step.get("intro_text", step.get("note", ""))),
                bool(step.get("is_terminal", False)),
                _s(step.get("terminal_action", step.get("action", "")), 64),
                bool(step.get("is_sub_procedure", False)),
                _s(step.get("sub_procedure_name", ""), 256),
                "",
                _s(step.get("narrative_context", "")),
                bool(step.get("is_out_of_scope", False)),
                _s(step.get("yaml_rule_id", ""), 64),
            ),
        )
        if not rows:
            continue
        step_db_id = rows[0][0]

        # Parser uses "decision_rows" key; fallback to "rows" for compatibility
        decision_rows = step.get("decision_rows") or step.get("rows") or []
        for ridx, row in enumerate(decision_rows):
            eob = row.get("eob_codes") or []
            ex = row.get("ex_codes") or []
            denial = row.get("denial_codes") or []
            sys_act = row.get("system_actions") or []

            # Fallback: classify codes from flat "codes" list
            if not (eob or ex or denial or sys_act):
                for c in row.get("codes") or []:
                    ct = _classify_code(c)
                    if ct == "EOB":
                        eob.append(c)
                    elif ct == "EX":
                        ex.append(c)
                    elif ct == "DENIAL":
                        denial.append(c)
                    elif ct == "SYSTEM_ACT":
                        sys_act.append(c)

            all_c = _merge_codes(eob, ex, denial, sys_act)
            dtype = _s(
                row.get("decision") or row.get("decision_type") or "", 16
            ).upper()
            if dtype not in VALID_DECISIONS:
                action = _s(row.get("action") or row.get("then") or "")
                dtype = _classify_decision(action, denial, eob)

            _exec(
                cfg,
                dec_sql,
                (
                    step_db_id,
                    ridx,
                    _s(row.get("condition_if", row.get("if", ""))),
                    _s(row.get("condition_and", row.get("and", ""))),
                    _s(row.get("action", row.get("then", ""))),
                    _s(row.get("action_summary", "")),
                    _s(row.get("action_line", "")),
                    _s(row.get("action_claim", "")),
                    dtype,
                    _i(row.get("skip_to_step") or row.get("goto_step")),
                    bool(row.get("is_terminal") or row.get("is_final", False)),
                    _j(eob),
                    _j(ex),
                    _j(denial),
                    _j(sys_act),
                    _j(all_c),
                    "",
                    bool(row.get("is_out_of_scope", False)),
                    _s(row.get("applicable_when", "")),
                ),
            )
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
    for gr in state.get("group_rules") or []:
        calc = _s(
            gr.get("calculation_from") or gr.get("calculation_basis") or "DOS", 32
        ).upper()
        if calc not in {"DOS", "PAID_DATE", "EOB_DATE"}:
            calc = "DOS"
        _exec(
            cfg,
            sql,
            (
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
            ),
        )
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

    VALID_TYPES = {
        "EOB",
        "EX",
        "DENIAL",
        "SYSTEM_ACT",
        "POS",
        "REVENUE",
        "BILL_TYPE",
        "MODIFIER",
        "FREQUENCY",
        "CPT",
        "UNKNOWN",
    }
    sql = """
        INSERT INTO sop_ingestion_auditcode
            (sop_id, code_value, code_type, description,
             context_snippet, source_step, source_field, confidence)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT ON CONSTRAINT unique_auditcode DO NOTHING;
    """
    for c in state.get("detected_codes") or []:
        val = _s(c.get("raw_value") or c.get("code") or c.get("value") or "", 64)
        if not val:
            continue
        ctype = _s(c.get("code_system") or c.get("code_type") or "", 16).upper()
        if ctype not in VALID_TYPES:
            ctype = _classify_code(val)
        _exec(
            cfg,
            sql,
            (
                sop_id,
                val,
                ctype,
                _s(c.get("description", "")),
                _s(c.get("context_snippet") or c.get("context") or ""),
                _i(c.get("source_step")),
                _s(c.get("source_field", ""), 256),
                float(c.get("confidence", 1.0)),
            ),
        )
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
    for dc in state.get("detected_date_conditions") or []:
        _exec(
            cfg,
            sql,
            (
                sop_id,
                _s(dc.get("date_from", ""), 32),
                _s(dc.get("date_to", ""), 32),
                _s(dc.get("effective_date", ""), 32),
                _s(dc.get("context") or dc.get("context_text") or ""),
                _s(dc.get("source_field") or dc.get("applies_to") or "", 256),
            ),
        )
    return {}


def pg_annotation_writer(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """
    Write AuditAnnotation rows — notes, alerts, exceptions.
    The auditor MUST read these before making a final claim decision.
    """
    sop_id = state.get("sop_db_id")
    if not sop_id:
        return {}

    VALID = {"NOTE", "ALERT", "EXCEPTION", "TIP", "WARNING", "HIGHLIGHT"}
    sql = """
        INSERT INTO sop_ingestion_auditannotation
            (sop_id, step_id, annotation_type, content_text, is_claim_impact)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING;
    """
    IMPACT_KEYWORDS = {
        "DENY",
        "BYPASS",
        "CODE",
        "PEND",
        "OVERRIDE",
        "E51",
        "F51",
        "346",
    }

    for ann in state.get("annotations") or []:
        atype = _s(ann.get("type") or ann.get("annotation_type") or "NOTE", 16).upper()
        if atype not in VALID:
            atype = "NOTE"
        content = _s(
            ann.get("text") or ann.get("content") or ann.get("content_text") or ""
        )
        if not content:
            continue
        impact = bool(
            ann.get("is_claim_impact")
            or any(k in content.upper() for k in IMPACT_KEYWORDS)
        )
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
    for lnk in state.get("links") or []:
        href = _s(lnk.get("href") or lnk.get("url") or "")
        text = _s(lnk.get("link_text") or lnk.get("text") or "")
        ltype = _s(lnk.get("link_type") or "UNRESOLVED", 32)
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
        log.info(
            "pg_graph_writer: no agentic graph in state — used deterministic builder"
        )

    if not nodes:
        log.info("pg_graph_writer: no nodes from any source — skipping")
        return {}

    from collections import Counter as _Counter

    type_counts = _Counter(n.get("type", "?") for n in nodes)
    log.info("pg_graph_writer[%s]: producing %s", source, dict(type_counts))

    # Clear any previous graph rows for this SOP so re-ingest is clean.
    _exec(
        cfg,
        """
        DELETE FROM sop_ingestion_auditgraphedge WHERE sop_id = %s;
    """,
        (sop_id,),
    )
    _exec(
        cfg,
        """
        DELETE FROM sop_ingestion_auditgraphnode WHERE sop_id = %s;
    """,
        (sop_id,),
    )

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
        rows = _exec(
            cfg,
            node_sql,
            (
                sop_id,
                _s(n["key"], 128),
                _s(n["type"], 24),
                _s(n["label"], 255),
                _j(n.get("details", {})),
                _s(n.get("ref_table", ""), 64),
                _i(n.get("ref_id")),
                int(n.get("order", 0) or 0),
            ),
        )
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
        _exec(
            cfg,
            edge_sql,
            (
                sop_id,
                src,
                tgt,
                _s(e["rel"], 24),
                _s(e.get("label", ""), 255),
                _j(e.get("details", {})),
            ),
        )

    log.info(
        "pg_graph_writer[%s]: wrote %d nodes, %d edges (skipped %d) for sop %s",
        source,
        len(nodes),
        len(edges) - skipped,
        skipped,
        sop_id,
    )
    # NOTE: do NOT overwrite state["audit_graph_nodes/edges"] (those are the
    # actual node/edge lists from the synthesis stage). Return persistence
    # stats under distinct keys for any downstream consumers.
    return {
        "audit_graph_persisted_nodes": len(nodes),
        "audit_graph_persisted_edges": len(edges) - skipped,
        "audit_graph_source": source,
    }
