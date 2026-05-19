"""Public API — the only import most users need.

from uhc_sop_ingestion import SopIngestionPipeline

pipeline = SopIngestionPipeline()                       # auto-loads .env
result   = pipeline.run("https://example.com/sop.html")
print(result["total_docs"], result["total_rules"])
"""
from __future__ import annotations

import uuid
import logging
from pathlib import Path
from typing import Optional

from .config import PipelineConfig
from .graph import build_graph

logger = logging.getLogger(__name__)


class SopIngestionPipeline:
    """Entry point for the LangGraph SOP ingestion pipeline.

    Parameters
    ----------
    env_path : str or Path, optional
        Explicit path to a .env file. Omit to auto-discover .env by walking
        up from the current working directory.
    config : PipelineConfig, optional
        Pass a pre-built config instead of loading from .env.
    """

    def __init__(
        self,
        env_path: Optional[str | Path] = None,
        config: Optional[PipelineConfig] = None,
    ):
        self.cfg = config or PipelineConfig.from_env(env_path)
        self._graph = build_graph(self.cfg)

    def run(
        self,
        seed_url: str,
        job_id: Optional[str] = None,
        max_depth: int = 4,
        max_docs: int = 200,
    ) -> dict:
        """Run the full ingestion pipeline from a seed URL.

        Parameters
        ----------
        seed_url  : URL (http/https) or local file path to the root SOP document.
        job_id    : Optional job UUID. Generated automatically if omitted.
        max_depth : How many link-hops to follow. Default 4.
        max_docs  : Maximum total documents to ingest. Default 200.

        Returns
        -------
        dict : full LangGraph final state, which includes:
               - "final_summary"  — top-level metrics (total_docs, total_rules, …)
               - "all_documents"  — list of per-document result dicts
               - "errors"         — list of error dicts collected during the run
               Use ``state["final_summary"]`` for a compact summary.
        """
        if not seed_url:
            raise ValueError("seed_url must be provided")

        resolved_job_id = job_id or str(uuid.uuid4())

        # Attach a real-time Postgres logger to cfg so every stage and
        # every LLM call is recorded immediately via psycopg2 autocommit.
        try:
            from .pg_logger import PipelineLogger
            pg_logger = PipelineLogger(self.cfg, resolved_job_id)
            self.cfg._pg_logger = pg_logger
        except Exception as exc:
            logger.warning("PipelineLogger init failed (logging disabled): %s", exc)
            pg_logger = None
            self.cfg._pg_logger = None

        initial_state = {
            "seed_url":  seed_url.strip(),
            "job_id":    resolved_job_id,
            "max_depth": max_depth,
            "max_docs":  max_docs,
            # Accumulator fields must be initialised as empty lists
            "url_queue":       [],
            "visited_hashes":  [],
            "visited_urls":    [],
            "all_documents":   [],
            "errors":          [],
            "validation_warnings": [],
            "total_processed": 0,
        }

        logger.info("SopIngestionPipeline: starting job=%s seed=%s", resolved_job_id, seed_url)
        try:
            final_state = self._graph.invoke(initial_state)
        finally:
            # Flush LLM totals and close the dedicated logger connection.
            if pg_logger:
                try:
                    pg_logger.refresh_job_totals()
                except Exception as exc:
                    logger.warning("pg_logger.refresh_job_totals failed: %s", exc)
                pg_logger.close()
                self.cfg._pg_logger = None

        return final_state

    def stream(
        self,
        seed_url: str,
        job_id: Optional[str] = None,
        max_depth: int = 4,
        max_docs: int = 200,
    ):
        """Stream state updates for live progress monitoring.

        Yields (node_name, state_delta) tuples as each LangGraph node
        completes. Useful for streaming progress to a UI or log.
        """
        resolved_job_id = job_id or str(uuid.uuid4())

        try:
            from .pg_logger import PipelineLogger
            pg_logger = PipelineLogger(self.cfg, resolved_job_id)
            self.cfg._pg_logger = pg_logger
        except Exception as exc:
            logger.warning("PipelineLogger init failed (stream): %s", exc)
            pg_logger = None
            self.cfg._pg_logger = None

        initial_state = {
            "seed_url":  seed_url.strip(),
            "job_id":    resolved_job_id,
            "max_depth": max_depth,
            "max_docs":  max_docs,
            "url_queue": [], "visited_hashes": [], "visited_urls": [],
            "all_documents": [], "errors": [], "validation_warnings": [],
            "total_processed": 0,
        }
        try:
            for event in self._graph.stream(initial_state, stream_mode="updates"):
                for node_name, delta in event.items():
                    yield node_name, delta
        finally:
            if pg_logger:
                try:
                    pg_logger.refresh_job_totals()
                except Exception:
                    pass
                pg_logger.close()
                self.cfg._pg_logger = None
