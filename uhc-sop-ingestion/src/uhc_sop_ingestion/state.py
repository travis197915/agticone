"""LangGraph pipeline state.

Every field is Optional so any agent can update only what it touches.
Lists that accumulate across agents use Annotated[list, operator.add]
so that each partial state update appends rather than overwrites.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Optional, TypedDict


class PipelineState(TypedDict, total=False):
    # ── Job metadata ─────────────────────────────────────────────────────────
    job_id: str
    seed_url: str
    max_depth: int
    max_docs: int
    total_processed: int

    # ── BFS queue (accumulated across iterations) ─────────────────────────────
    # Each entry: {url, depth, parent_url, link_text, link_type}
    url_queue: Annotated[list[dict], operator.add]
    visited_hashes: Annotated[list[str], operator.add]  # content-hash dedup
    visited_urls: Annotated[list[str], operator.add]    # url-level dedup

    # ── Current document being processed ─────────────────────────────────────
    current_url: str
    current_depth: int
    current_parent_url: str

    # ── Fetch layer outputs ───────────────────────────────────────────────────
    raw_bytes_b64: str          # base64-encoded raw content (serialisable)
    doc_format: str             # HTML | DOCX | XLSX | PDF | UNKNOWN
    content_hash: str           # SHA256[:16]
    content_type: str
    encoding: str
    is_local: bool
    is_duplicate: bool

    # ── Versioning (revision-date tracking) ───────────────────────────────────
    canonical_url: str
    normalized_revision_date: str
    prior_sop_db_id: Optional[int]
    version_action: str           # NEW | UNCHANGED | REVISED | CONTENT_CHANGE
    version_registered: bool
    version_diff_id: Optional[int]
    version_diff_summary: dict
    trigger_source: str           # manual | workflow | revision_check
    requires_human_review: bool

    # ── Parse layer outputs ───────────────────────────────────────────────────
    metadata: dict              # title, effective_date, revision_date, platform…
    pre_sections: list[dict]    # [{name, order, items, annotations}]
    steps: list[dict]           # [{number, question, decision_rows, …}]
    # Deterministic BS4 step context store + checklist (a03.html_step_inventory)
    # — consumed by a07.step_checklist_reconciler so no step is ever dropped.
    step_inventory: list[dict]  # [{number, title, rows:[{cells}], raw_text}]
    step_checklist: list[int]   # sorted step numbers found in the document
    # PDF VISION DOOR (a06c/a06d/a06e) — native-PDF perception + graph-first
    # contextualization. Heavy payloads (page perception, entities, relations)
    # live on the Redis blackboard (sop:pdf:{job_id}:*), Mongo (raw audit trail)
    # and Neo4j (context graph); state carries only light references below.
    pdf_page_count: int             # merged page count after perception
    pdf_slice_count: int            # number of native-PDF slices sent to Claude
    pdf_context_graph_id: str       # "{job_id}:{content_hash}" of the Neo4j graph
    sub_procedures: list[dict]
    reference_tables: list[dict]
    group_rules: list[dict]
    annotations: list[dict]     # [{annotation_type, text, is_highlight}]
    links: list[dict]           # [{href, text, link_type, resolved_url}]
    raw_text: str
    xlsx_workbook_type: str
    parse_warnings: list[str]
    code_table_entries: list[dict]  # from XLSX/DOCX code tables

    # ── LLM enrichment outputs ────────────────────────────────────────────────
    enriched_steps: list[dict]
    # Normalised conditions of preamble exception/override rules that were
    # attached to their host step (pdf_exception_attacher) so the Postgres writer
    # does not also emit them as a standalone "Step 0 — Pre-Step Exceptions" node.
    exception_rules_attached: list[str]
    enriched_rules: list[dict]
    llm_summary: str
    llm_tokens_used: int

    # ── Context extraction outputs ────────────────────────────────────────────
    detected_codes: list[dict]          # [{raw_value, code_system, confidence, ctx}]
    detected_list_refs: list[dict]      # [{list_name, list_type, source_field}]
    detected_date_conditions: list[dict]# [{date_from, date_to, effective_date, ctx}]

    # ── Validation outputs ────────────────────────────────────────────────────
    validation_passed: bool
    validation_warnings: Annotated[list[str], operator.add]

    # ── Agentic graph-synthesis outputs (a16_graph_synthesis) ────────────────
    # Filled by agent_graph_assembler from per-agent Redis context.
    # Consumed by pg_graph_writer (PostgreSQL) and neo4j_graph_writer (Neo4j).
    audit_graph_nodes: list[dict]   # [{key, type, label, details, order}]
    audit_graph_edges: list[dict]   # [{source, target, rel, label, details}]
    audit_graph_persisted_nodes: int
    audit_graph_persisted_edges: int
    audit_graph_source: str         # "agentic_llm" | "deterministic_fallback"

    # ── Canonical IR synthesis outputs (a18_ir_synthesis) ────────────────────
    # The routing-complete SopIR (pure JSON data — same shape as the YAML SOPs).
    # Consumed by pipeline_runner -> sop_ir.persist.persist_ir (the shared write
    # gate). sop_ir_source records whether routing came from the deterministic
    # draft or the LLM enricher; sop_ir_validation holds the checker verdict.
    sop_ir: dict
    sop_ir_source: str
    sop_ir_validation: dict
    # One entry per processed document so multi-doc crawls persist every SOP's
    # IR (not just the last). Each: {content_hash, url, ir, source, validation}.
    # Keyed by content_hash so pipeline_runner can match it to its AuditSop row.
    sop_ir_documents: Annotated[list[dict], operator.add]

    # ── Write outputs ─────────────────────────────────────────────────────────
    neo4j_sop_id: str
    sop_db_id: Optional[int]      # PK of AuditSop row — passed to all child writers
    postgres_doc_id: Optional[int]
    mongo_raw_id: str
    mongo_parsed_id: str

    # ── Accumulated results ───────────────────────────────────────────────────
    # One dict per processed document, appended after each write stage
    all_documents: Annotated[list[dict], operator.add]

    # ── Error tracking ────────────────────────────────────────────────────────
    errors: Annotated[list[dict], operator.add]

    # ── Pipeline routing ──────────────────────────────────────────────────────
    format_routed: str          # which format branch was taken
    processing_complete: bool   # True → route to final_summary

    # ── Final output ─────────────────────────────────────────────────────────
    final_summary: dict

    # ── Generic API-call agent (a17_api_caller) ──────────────────────────────
    # Input fields (set by the caller before invoking the api_call stage):
    api_url: str                  # required — full URL incl. scheme
    api_method: str               # GET | POST | PUT | PATCH | DELETE (default GET)
    api_auth: dict                # {"type": "bearer"|"basic"|"api_key"|"none", ...}
    api_headers: dict             # extra request headers
    api_params: dict              # query-string params
    api_body: Any                 # dict → json-encoded, str/bytes sent as-is
    api_timeout: int              # seconds, default 30

    # Output fields (set by the api_call agents):
    api_call_id: str              # stable id for this single call (uuid)
    api_credential_id: Optional[int]    # PK of saved credential row in PG
    api_response_id: str          # Mongo `_id` of the saved response document
    api_response_status: int      # HTTP status code
    api_response_headers: dict    # response headers
    api_response_text: str        # raw response body (truncated for state)
    api_response_json: Any        # parsed JSON (dict | list | None)
    api_response_is_json: bool    # True if body parsed cleanly as JSON
    api_request_started_at: float # epoch seconds
    api_request_elapsed_ms: int   # wall-clock latency
    api_response_error: str       # network / parse error message, if any
