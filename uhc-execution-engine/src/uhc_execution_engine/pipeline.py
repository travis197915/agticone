"""Public API for running the rule engine on a single claim."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from .claim_fetcher import resolve_fetch_tool
from .config import get_config
from .graph import build_graph
from .llm import execution_run_context
from .memory import load_prior_context
from .mcp_client import (check_mcp_health, format_mcp_health_error,
                         should_run_mcp_health_check)
from .rule_loader import load_workflow_bindings
from .state import ExecutionState

logger = logging.getLogger(__name__)


class RuleEnginePipeline:
    """One-call entrypoint: ``run(workflow_id, claim) -> dict``.

    Intended to be invoked from inside a Django request/worker process so
    the agent_tools registry and ORM models are importable.
    """

    def __init__(self):
        self._graph = build_graph()

    def run(self, *, workflow_id: str, claim: dict[str, Any],
            raw_fetch: dict[str, Any] | None = None,
            claim_id: str = "",
            batch_id: str | None = None,
            run_id: str | None = None,
            tool_results: dict[str, dict[str, Any]] | None = None,
            fetch_tool: str | None = None,
            skip_mcp_health_check: bool = False) -> dict[str, Any]:
        # Mint the run_id up here (instead of inside n01_validate) so we can
        # stamp it on every LLMCallLog row via the contextvar set below.
        run_id = run_id or str(uuid.uuid4())
        # Optional pre-seeded tool results, keyed by NodeToolBinding id. When a
        # binding already has a result here, ``run_tools`` skips the live
        # invocation and reuses it. Lets callers inject already-fetched/cached
        # tool payloads; default empty preserves the original fetch-everything
        # behaviour.
        resolved_claim_id = claim_id or str(
            claim.get("claim_id") or claim.get("subscriber_id") or "")

        if not skip_mcp_health_check:
            resolved_fetch_tool = fetch_tool
            if not resolved_fetch_tool:
                try:
                    loaded = load_workflow_bindings(str(workflow_id))
                    resolved_fetch_tool = resolve_fetch_tool(
                        str(workflow_id), loaded["all_tool_bindings"],
                    )
                except Exception:
                    resolved_fetch_tool = None
            if should_run_mcp_health_check(fetch_tool=resolved_fetch_tool):
                health = check_mcp_health(
                    tool_name=resolved_fetch_tool,
                    claim_id=resolved_claim_id,
                )
                if not health.get("ok"):
                    error_message = format_mcp_health_error(health)
                    logger.warning(
                        "rule_engine: MCP health check failed claim=%s error=%s",
                        resolved_claim_id or "-",
                        health.get("error") or "-",
                    )
                    return {
                        "run_id": run_id,
                        "claim_id": resolved_claim_id,
                        "status": "FAILED",
                        "error_message": error_message,
                        "final_decision_type": "",
                        "applied_codes": [],
                        "narrative": "",
                        "evaluations": [],
                        "tool_invocations": [],
                        "stages": [],
                    }

        # Persistent per-claim context, one memory row per (claim_id, SOP).
        # Loaded here — the single entry point for every claim run — so batch
        # and single-claim paths are both memory-aware. Fail-open: {} runs cold.
        try:
            prior_context = load_prior_context(
                get_config(), claim_id=resolved_claim_id, claim=claim or {})
        except Exception:  # pragma: no cover - memory must never block a run
            logger.exception("rule_engine: load_prior_context failed; "
                             "running cold for claim_id=%s", resolved_claim_id)
            prior_context = {}
        initial: ExecutionState = {
            "workflow_id": str(workflow_id),
            "claim": claim or {},
            "raw_fetch": raw_fetch or {},
            "claim_id": resolved_claim_id,
            "batch_id": batch_id,
            "stages": [],
            "tool_invocations": [],
            "tool_results": tool_results or {},
            "prior_context": prior_context,
            "drift_entries": [],
            "status": "RUNNING",
            "run_id": run_id,
        }
        try:
            with execution_run_context(run_id):
                final_state = self._graph.invoke(initial)
        except Exception as exc:
            logger.exception("rule_engine: pipeline crashed")
            return {
                "run_id": run_id,
                "claim_id": initial["claim_id"],
                "status": "FAILED",
                "error_message": f"pipeline crashed: {exc}",
                "final_decision_type": "",
                "applied_codes": [],
                "narrative": "",
                "evaluations": [],
                "tool_invocations": initial["tool_invocations"],
                "stages": initial["stages"],
            }
        return final_state.get("response") or {"status": "FAILED",
                                                "error_message": "no response built"}
