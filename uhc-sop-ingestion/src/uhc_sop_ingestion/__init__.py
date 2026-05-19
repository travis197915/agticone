"""uhc-sop-ingestion — LangGraph multi-agent SOP ingestion pipeline.

Quick start:
    from uhc_sop_ingestion import SopIngestionPipeline

    pipeline = SopIngestionPipeline()   # loads .env automatically
    state    = pipeline.run("https://example.com/path/to/sop.html")

    summary = state["final_summary"]
    print(summary["total_docs"], summary["total_rules"], summary["total_codes"])
    print(len(state["all_documents"]), "documents processed")
    print(state["errors"])              # list of any per-document errors
"""
from .pipeline import SopIngestionPipeline
from .config   import PipelineConfig, load_env

__all__ = ["SopIngestionPipeline", "PipelineConfig", "load_env"]
