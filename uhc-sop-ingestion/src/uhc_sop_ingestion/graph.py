"""LangGraph StateGraph — wires all 121 agent functions into one pipeline.

Graph structure (BFS loop with conditional routing):

  START
    │
  [INTAKE] validate_url → url_normalizer → job_initializer
           → redis_job_tracker → mongo_job_logger
    │
  ┌─────────────────────────────────────────────────┐
  │ BFS LOOP                                        │
  │                                                 │
  │  pick_next_url                                  │
  │      │ (has URL?)                               │
  │      ├── NO → final_stage                       │
  │      └── YES                                    │
  │          │                                      │
  │      fetch_stage (9 fetch agents)               │
  │          │ (duplicate?)                         │
  │          ├── YES → link_stage (back to loop)    │
  │          └── NO                                 │
  │              │                                  │
  │          [CONDITIONAL: doc_format]              │
  │          ├── HTML → html_parse_stage            │
  │          ├── DOCX → docx_parse_stage            │
  │          ├── XLSX → xlsx_parse_stage            │
  │          └── PDF  → pdf_parse_stage             │
  │              │                                  │
  │          enrich_stage (10 LLM agents)           │
  │              │                                  │
  │          context_stage (12 context agents)      │
  │              │                                  │
  │          validate_stage (6 validation agents)   │
  │              │                                  │
  │          write_stage (Neo4j + PG + Mongo +Redis)│
  │              │                                  │
  │          link_stage (8 link agents)             │
  │              │                                  │
  │          completion_check                       │
  │              │ (more work?)                     │
  └──────────────┘                                  │
                                                    │
  final_stage → job_closer → END
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Literal

_log = logging.getLogger(__name__)

from langgraph.graph import END, START, StateGraph

from .state import PipelineState
from .config import PipelineConfig

# ── Import all agent functions ────────────────────────────────────────────────
from .agents.a01_intake import (
    url_validator,
    url_normalizer,
    job_initializer,
    redis_job_tracker,
    mongo_job_logger,
)
from .agents.a02_fetch import (
    next_url_picker,
    depth_limit_checker,
    http_fetcher,
    local_file_fetcher,
    content_type_detector,
    extension_detector,
    magic_bytes_detector,
    content_hasher,
    duplicate_checker,
)
from .agents.a03_parse_html import (
    html_decode,
    html_metadata,
    html_biz_table,
    html_pre_sections,
    html_steps,
    html_step_inventory,
    html_decision_tables,
    html_compound_tables,
    html_group_tables,
    html_annotations,
    html_reference_tables,
    html_sub_procedures,
    html_links,
)
from .agents.a03b_html_graph import (
    html_perceive,
    html_entity_extractor,
    html_relation_reasoner,
    html_context_graph_writer,
    html_context_validator,
    html_step_synthesizer,
    html_presection_synthesizer,
    html_quality_gate,
)
from .agents.a04_parse_docx import (
    docx_metadata,
    docx_headings,
    docx_paragraphs,
    docx_tables,
    docx_code_tables,
    docx_hyperlinks,
)
from .agents.a05_parse_xlsx import (
    xlsx_workbook_type,
    xlsx_sheet_parser,
    xlsx_header_detector,
    xlsx_code_extractor,
    xlsx_calculator,
    xlsx_metadata,
)
from .agents.a06_parse_pdf import pdf_metadata

# PDF VISION DOOR — native-PDF perception + graph-first contextualization +
# graph->canonical synthesis. Fully replaces the old pypdf text army.
from .agents.a06c_pdf_perception import (
    pdf_slicer,
    pdf_page_reader,
    pdf_perception_merger,
)
from .agents.a06d_pdf_context_graph import (
    pdf_entity_extractor,
    pdf_relation_reasoner,
    pdf_context_graph_writer,
    pdf_context_validator,
    pdf_step_reconciler,
)
from .agents.a06e_pdf_synthesis import (
    pdf_step_synthesizer,
    pdf_presection_synthesizer,
    pdf_exception_attacher,
    pdf_quality_gate,
)
from .agents.a07_enrich import (
    step_checklist_reconciler,
    step_question_refiner,
    decision_row_classifier,
    rule_semantic_enricher,
    cross_reference_resolver,
    ambiguous_term_resolver,
    potf_validator,
    pre_section_rule_extractor,
    group_rule_extractor,
    date_condition_extractor,
    summary_generator,
)
from .agents.a08_context import (
    eob_code_detector,
    ex_code_detector,
    denial_code_detector,
    pos_code_detector,
    revenue_code_detector,
    bill_type_detector,
    modifier_code_detector,
    frequency_code_detector,
    system_action_detector,
    cpt_code_detector,
    entity_list_ref_detector,
    code_deduplicator,
)
from .agents.a09_validate import (
    document_completeness,
    step_sequence,
    decision_row_check,
    code_system_check,
    link_validator,
    metadata_validator,
)
from .agents.a10_write_neo4j import (
    god_node_writer,
    pre_section_node_writer,
    step_node_writer,
    rule_node_writer,
    annotation_node_writer,
    code_node_writer,
    group_rule_node_writer,
    sequential_edge_writer,
    branch_edge_writer,
    child_doc_edge_writer,
    reference_table_node_writer,
    group_rule_step_edge_writer,
    sub_procedure_node_writer,
    neo4j_graph_writer,
    html_dom_writer,
)
from .agents.a11_write_postgres import (
    pg_sop_writer,
    pg_precondition_writer,
    pg_step_writer,
    pg_group_limit_writer,
    pg_code_writer,
    pg_date_condition_writer,
    pg_annotation_writer,
    pg_reference_writer,
    pg_job_updater,
    pg_graph_writer,
)
from .agents.a12_write_mongo import (
    mongo_raw_writer,
    mongo_parsed_writer,
    mongo_job_progress,
)
from .agents.a13_write_redis import (
    redis_cache_writer,
    redis_queue_manager,
    redis_progress_tracker,
)
from .agents.a14_links import (
    link_classifier,
    html_link_queue,
    docx_link_queue,
    xlsx_link_queue,
    pdf_link_queue,
    unresolved_link_logger,
    internal_anchor_mapper,
    accumulated_doc_appender,
)
from .agents.a15_control import (
    completion_checker,
    state_clearer,
    error_handler,
    final_summary,
    job_closer,
)
from .agents.a16_graph_synthesis import (
    agent_document_profiler,
    agent_pre_section_synthesizer,
    agent_step_decomposer,
    agent_decision_classifier,
    agent_code_grounder,
    agent_reference_resolver,
    agent_semantic_edge_reasoner,
    agent_graph_assembler,
)
from .agents.a17_narrative import sop_overview_narrator, step_narrative_writer
from .agents.a18_ir_synthesis import ir_maker, ir_checker


def _bind(fn, cfg: PipelineConfig):
    """Bind cfg into an agent function so LangGraph sees (state) -> dict."""

    @functools.wraps(fn)
    def wrapper(state: PipelineState) -> dict:
        return fn(state, cfg) or {}

    return wrapper


def _stage(*fns, cfg: PipelineConfig, stage_name: str = "unknown"):
    """Chain multiple agent functions into one LangGraph node.

    Wraps execution with real-time Postgres stage logging when a
    PipelineLogger is attached to cfg as ``cfg._pg_logger``.
    """
    bound = [_bind(fn, cfg) for fn in fns]

    def node(state: PipelineState) -> dict:
        pg_logger = getattr(cfg, "_pg_logger", None)
        doc_url = state.get("current_url", "") or state.get("seed_url", "")
        doc_format = state.get("doc_format", "")
        doc_depth = state.get("current_depth")

        row_id = None
        t0 = time.time()
        if pg_logger:
            row_id = pg_logger.log_stage_start(
                stage_name,
                doc_url=doc_url,
                doc_format=doc_format,
                doc_depth=doc_depth,
            )

        updates: dict = {}
        current = dict(state)
        try:
            for fn in bound:
                delta = fn(current)
                if delta:
                    updates.update(delta)
                    current.update(delta)
            if pg_logger:
                pg_logger.log_stage_end(row_id, status="OK", started_ts=t0)
        except Exception as exc:
            _log.exception("Stage %s failed: %s", stage_name, exc)
            if pg_logger:
                pg_logger.log_stage_end(
                    row_id, status="ERROR", error_detail=str(exc), started_ts=t0
                )
            raise
        return updates

    return node


# ── Routing functions ─────────────────────────────────────────────────────────


def _route_after_fetch(state: PipelineState) -> str:
    if state.get("is_duplicate"):
        return "link_stage"
    fmt = state.get("doc_format", "")
    return {
        "HTML": "html_parse",
        "DOCX": "docx_parse",
        "XLSX": "xlsx_parse",
        "PDF": "pdf_perceive",
    }.get(fmt, "html_parse")


def _route_completion(state: PipelineState) -> str:
    return "final_stage" if state.get("processing_complete") else "pick_next_url"


def _route_after_pick(state: PipelineState) -> str:
    return "final_stage" if state.get("processing_complete") else "fetch_stage"


# ── Graph builder ─────────────────────────────────────────────────────────────


def build_graph(cfg: PipelineConfig) -> StateGraph:
    g = StateGraph(PipelineState)

    # ── Intake ────────────────────────────────────────────────────────────────
    g.add_node(
        "intake_stage",
        _stage(
            url_validator,
            url_normalizer,
            job_initializer,
            redis_job_tracker,
            mongo_job_logger,
            cfg=cfg,
            stage_name="intake_stage",
        ),
    )

    # ── BFS pick ──────────────────────────────────────────────────────────────
    g.add_node(
        "pick_next_url", _stage(next_url_picker, cfg=cfg, stage_name="pick_next_url")
    )

    # ── Fetch ─────────────────────────────────────────────────────────────────
    g.add_node(
        "fetch_stage",
        _stage(
            depth_limit_checker,
            http_fetcher,
            local_file_fetcher,
            content_type_detector,
            extension_detector,
            magic_bytes_detector,
            content_hasher,
            duplicate_checker,
            cfg=cfg,
            stage_name="fetch_stage",
        ),
    )

    # ── Parse ─────────────────────────────────────────────────────────────────
    g.add_node(
        "html_parse",
        _stage(
            html_decode,
            html_metadata,
            html_biz_table,
            html_pre_sections,
            html_steps,
            html_step_inventory,
            html_decision_tables,
            html_compound_tables,
            html_group_tables,
            html_annotations,
            html_reference_tables,
            html_sub_procedures,
            html_links,
            cfg=cfg,
            stage_name="html_parse",
        ),
    )
    # HTML GRAPH DOOR — the SEPARATE HTML analog of the PDF vision door. Mirrors
    # PDF's perceive → contextualize → synthesize but is driven by the structured
    # DOM (deterministic perception). Shares NO code with the PDF agents.
    # 1) contextualize: DOM -> pages -> entities/relations -> durable Neo4j
    #    context graph (:HtmlDoc/:HtmlNode), with a per-page coverage retry.
    g.add_node(
        "html_contextualize",
        _stage(
            html_perceive,
            html_entity_extractor,
            html_relation_reasoner,
            html_context_graph_writer,
            html_context_validator,
            cfg=cfg,
            stage_name="html_contextualize",
        ),
    )
    # 2) synthesize: pages/entities -> canonical steps/pre_sections (nested
    #    decision rows), then the shared format-agnostic enrichers add group
    #    rules, date conditions and the summary, then the SopIR quality gate.
    g.add_node(
        "html_synthesize",
        _stage(
            html_step_synthesizer,
            html_presection_synthesizer,
            pre_section_rule_extractor,
            group_rule_extractor,
            date_condition_extractor,
            summary_generator,
            html_quality_gate,
            cfg=cfg,
            stage_name="html_synthesize",
        ),
    )
    g.add_node(
        "docx_parse",
        _stage(
            docx_metadata,
            docx_headings,
            docx_paragraphs,
            docx_tables,
            docx_code_tables,
            docx_hyperlinks,
            cfg=cfg,
            stage_name="docx_parse",
        ),
    )
    g.add_node(
        "xlsx_parse",
        _stage(
            xlsx_workbook_type,
            xlsx_sheet_parser,
            xlsx_header_detector,
            xlsx_code_extractor,
            xlsx_calculator,
            xlsx_metadata,
            cfg=cfg,
            stage_name="xlsx_parse",
        ),
    )
    # PDF VISION DOOR — three dedicated stages replace pdf_parse + pdf_enrich.
    # 1) perceive: read every page natively via Claude, stitch cross-page.
    g.add_node(
        "pdf_perceive",
        _stage(
            pdf_metadata,
            pdf_slicer,
            pdf_page_reader,
            pdf_perception_merger,
            cfg=cfg,
            stage_name="pdf_perceive",
        ),
    )
    # 2) contextualize: build the durable Neo4j context graph BEFORE extraction.
    g.add_node(
        "pdf_contextualize",
        _stage(
            pdf_entity_extractor,
            pdf_relation_reasoner,
            pdf_context_graph_writer,
            pdf_context_validator,
            pdf_step_reconciler,
            cfg=cfg,
            stage_name="pdf_contextualize",
        ),
    )
    # 3) synthesize: graph -> canonical steps/pre_sections (nested decision
    #    rows), then the shared format-agnostic enrichers add group rules, date
    #    conditions and the summary, then the SopIR Pydantic gate.
    g.add_node(
        "pdf_synthesize",
        _stage(
            pdf_step_synthesizer,
            pdf_presection_synthesizer,
            pre_section_rule_extractor,
            group_rule_extractor,
            date_condition_extractor,
            summary_generator,
            pdf_quality_gate,
            cfg=cfg,
            stage_name="pdf_synthesize",
        ),
    )

    # ── LLM enrichment ────────────────────────────────────────────────────────
    g.add_node(
        "enrich_stage",
        _stage(
            step_checklist_reconciler,
            step_question_refiner,
            decision_row_classifier,
            rule_semantic_enricher,
            cross_reference_resolver,
            ambiguous_term_resolver,
            potf_validator,
            pre_section_rule_extractor,
            group_rule_extractor,
            date_condition_extractor,
            summary_generator,
            pdf_exception_attacher,
            cfg=cfg,
            stage_name="enrich_stage",
        ),
    )

    # ── Context extraction ────────────────────────────────────────────────────
    g.add_node(
        "context_stage",
        _stage(
            eob_code_detector,
            ex_code_detector,
            denial_code_detector,
            pos_code_detector,
            revenue_code_detector,
            bill_type_detector,
            modifier_code_detector,
            frequency_code_detector,
            system_action_detector,
            cpt_code_detector,
            entity_list_ref_detector,
            code_deduplicator,
            cfg=cfg,
            stage_name="context_stage",
        ),
    )

    # ── Validation ────────────────────────────────────────────────────────────
    g.add_node(
        "validate_stage",
        _stage(
            document_completeness,
            step_sequence,
            decision_row_check,
            code_system_check,
            link_validator,
            metadata_validator,
            cfg=cfg,
            stage_name="validate_stage",
        ),
    )

    # ── Narrative context (LLM-driven, story-style summaries) ────────────────
    # Generates a SOP-level overview narrative and a per-step paragraph that
    # explains purpose / decision walkthrough / next-step routing. Persisted
    # to ``AuditSop.narrative_context`` and ``AuditStep.narrative_context``
    # by the PG writer downstream, and surfaced in the SPA rule picker so
    # the user understands what each rule actually means.
    g.add_node(
        "narrative_stage",
        _stage(
            sop_overview_narrator,
            step_narrative_writer,
            cfg=cfg,
            stage_name="narrative_stage",
        ),
    )

    # ── Agentic graph synthesis (LLM-driven, Redis shared context) ───────────
    # GPT-4o builds structural nodes, Claude infers semantic edges. The
    # assembler validates the result and falls back to the deterministic
    # builder if validation fails so PG/Neo4j writers always have a graph.
    g.add_node(
        "graph_synthesis_stage",
        _stage(
            agent_document_profiler,
            agent_pre_section_synthesizer,
            agent_step_decomposer,
            agent_decision_classifier,
            agent_code_grounder,
            agent_reference_resolver,
            agent_semantic_edge_reasoner,
            agent_graph_assembler,
            cfg=cfg,
            stage_name="graph_synthesis_stage",
        ),
    )

    # ── Canonical IR synthesis (maker + checker, Redis sop:ir blackboard) ────
    # Synthesizes the same routing-complete IR a hand-authored YAML produces, so
    # an ingested SOP routes identically in the engine and appears as a workflow
    # in the builder UI. Emits state["sop_ir"] (pure data); the authoritative
    # relational write happens in pipeline_runner via the shared persist_ir gate.
    g.add_node(
        "ir_synthesis_stage",
        _stage(
            ir_maker,
            ir_checker,
            cfg=cfg,
            stage_name="ir_synthesis_stage",
        ),
    )

    # ── Write — Neo4j ─────────────────────────────────────────────────────────
    # html_dom_writer runs LAST in the stage so the HtmlBlock subgraph
    # is in place by the time neo4j_graph_writer tries to MERGE the
    # :DERIVED_FROM cross-edges. (html_dom_writer also backfills the same
    # cross-edges itself, making the ordering between the two writers safe
    # either way.)
    g.add_node(
        "write_neo4j",
        _stage(
            god_node_writer,
            pre_section_node_writer,
            step_node_writer,
            rule_node_writer,
            annotation_node_writer,
            code_node_writer,
            group_rule_node_writer,
            sequential_edge_writer,
            branch_edge_writer,
            child_doc_edge_writer,
            reference_table_node_writer,
            group_rule_step_edge_writer,
            sub_procedure_node_writer,
            neo4j_graph_writer,  # canonical knowledge-graph in Neo4j
            html_dom_writer,  # HTML DOM mirror + :DERIVED_FROM cross-edges
            cfg=cfg,
            stage_name="write_neo4j",
        ),
    )

    # ── Write — Postgres ──────────────────────────────────────────────────────
    g.add_node(
        "write_postgres",
        _stage(
            pg_sop_writer,
            pg_precondition_writer,
            pg_step_writer,
            pg_group_limit_writer,
            pg_code_writer,
            pg_date_condition_writer,
            pg_annotation_writer,
            pg_reference_writer,
            pg_graph_writer,  # canonical knowledge-graph in Postgres
            pg_job_updater,
            cfg=cfg,
            stage_name="write_postgres",
        ),
    )

    # ── Write — MongoDB + Redis ────────────────────────────────────────────────
    g.add_node(
        "write_mongo",
        _stage(
            mongo_raw_writer,
            mongo_parsed_writer,
            mongo_job_progress,
            cfg=cfg,
            stage_name="write_mongo",
        ),
    )
    g.add_node(
        "write_redis",
        _stage(
            redis_cache_writer,
            redis_queue_manager,
            redis_progress_tracker,
            cfg=cfg,
            stage_name="write_redis",
        ),
    )

    # ── Link discovery ────────────────────────────────────────────────────────
    g.add_node(
        "link_stage",
        _stage(
            link_classifier,
            html_link_queue,
            docx_link_queue,
            xlsx_link_queue,
            pdf_link_queue,
            unresolved_link_logger,
            internal_anchor_mapper,
            accumulated_doc_appender,
            cfg=cfg,
            stage_name="link_stage",
        ),
    )

    # ── Completion + reset ────────────────────────────────────────────────────
    g.add_node(
        "completion_check",
        _stage(
            error_handler,
            completion_checker,
            state_clearer,
            cfg=cfg,
            stage_name="completion_check",
        ),
    )

    # ── Final ─────────────────────────────────────────────────────────────────
    g.add_node(
        "final_stage",
        _stage(
            final_summary,
            job_closer,
            cfg=cfg,
            stage_name="final_stage",
        ),
    )

    # ── Edges ─────────────────────────────────────────────────────────────────
    g.add_edge(START, "intake_stage")
    g.add_edge("intake_stage", "pick_next_url")

    g.add_conditional_edges(
        "pick_next_url",
        _route_after_pick,
        {"fetch_stage": "fetch_stage", "final_stage": "final_stage"},
    )
    g.add_conditional_edges(
        "fetch_stage",
        _route_after_fetch,
        {
            "html_parse": "html_parse",
            "docx_parse": "docx_parse",
            "xlsx_parse": "xlsx_parse",
            "pdf_perceive": "pdf_perceive",
            "link_stage": "link_stage",
        },
    )

    # Parse → enrich → context → validate → write (sequential)
    # DOCX/XLSX use the shared HTML enrich_stage. HTML and PDF each run their own
    # dedicated graph-first door (contextualize → synthesize) so the three flows
    # never mix. All branches rejoin at context_stage.
    for parse_node in ("docx_parse", "xlsx_parse"):
        g.add_edge(parse_node, "enrich_stage")
    # HTML door: keep html_parse (metadata/links/DOM-mirror/biz-table) then run
    # the separate HTML graph door which overwrites steps/pre_sections.
    g.add_edge("html_parse", "html_contextualize")
    g.add_edge("html_contextualize", "html_synthesize")
    g.add_edge("html_synthesize", "context_stage")
    g.add_edge("pdf_perceive", "pdf_contextualize")
    g.add_edge("pdf_contextualize", "pdf_synthesize")
    g.add_edge("enrich_stage", "context_stage")
    g.add_edge("pdf_synthesize", "context_stage")
    g.add_edge("context_stage", "validate_stage")
    g.add_edge("validate_stage", "narrative_stage")
    g.add_edge("narrative_stage", "graph_synthesis_stage")
    g.add_edge("graph_synthesis_stage", "ir_synthesis_stage")
    g.add_edge("ir_synthesis_stage", "write_neo4j")
    g.add_edge("write_neo4j", "write_postgres")
    g.add_edge("write_postgres", "write_mongo")
    g.add_edge("write_mongo", "write_redis")
    g.add_edge("write_redis", "link_stage")
    g.add_edge("link_stage", "completion_check")

    g.add_conditional_edges(
        "completion_check",
        _route_completion,
        {"pick_next_url": "pick_next_url", "final_stage": "final_stage"},
    )
    g.add_edge("final_stage", END)

    return g.compile()
