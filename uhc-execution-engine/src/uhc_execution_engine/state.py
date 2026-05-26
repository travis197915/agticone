"""LangGraph state for the inner (per-claim) pipeline."""
from __future__ import annotations

from typing import Any, Optional, TypedDict


class ExecutionState(TypedDict, total=False):
    # Inputs
    run_id: str
    workflow_id: str
    claim: dict[str, Any]                         # parsed claim, fetched upstream
    raw_fetch: dict[str, Any]                     # raw linx_claim_search output
    batch_id: Optional[str]                       # set when run from BatchRunner
    claim_id: str                                 # the id we keyed off

    # load_bindings outputs
    preconditions: list[dict[str, Any]]           # hydrated rule dicts
    decisions: list[dict[str, Any]]
    tools_by_rule_key: dict[str, list[dict[str, Any]]]
    tools_by_shape: dict[str, list[dict[str, Any]]]

    # run_tools outputs (binding_id -> tool result)
    tool_results: dict[str, dict[str, Any]]
    tool_invocations: list[dict[str, Any]]        # ordered log for response/persistence

    # Evaluation outputs
    precondition_results: list[dict[str, Any]]
    decision_results: list[dict[str, Any]]
    terminate: bool                               # set when blocking precondition fails

    # Aggregation outputs
    final_decision_type: str
    applied_codes: list[str]
    narrative: str

    # Control / persistence
    status: str                                   # RUNNING|COMPLETED|FAILED|TERMINATED_BY_PRECONDITION
    error_message: str
    stages: list[dict[str, Any]]                  # per-node telemetry
    response: dict[str, Any]                      # final dict returned to caller
