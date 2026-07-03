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
    execution_mode: str                           # "linear" (default) | "parallel"
    claim_lob: dict[str, Any]                     # {product, network, label, source}
    lob_out_of_scope: bool                        # claim LOB not in workflow's supported set

    # load_bindings outputs
    preconditions: list[dict[str, Any]]           # hydrated rule dicts (kept for back-compat)
    decisions: list[dict[str, Any]]               # kept for back-compat
    shapes: list[dict[str, Any]]                  # per-Shape groupings (rules + tools)
    tools_by_rule_key: dict[str, list[dict[str, Any]]]
    tools_by_shape: dict[str, list[dict[str, Any]]]

    # Persistent per-claim context loaded at pipeline start (see memory.py).
    # Empty dict when memory is disabled or this claim has no prior runs.
    prior_context: dict[str, Any]
    # Structured live-vs-prior disagreements recorded this run; appended to
    # ClaimMemory.drift on persist.
    drift_entries: list[dict[str, Any]]

    # run_tools outputs (binding_id -> tool result)
    tool_results: dict[str, dict[str, Any]]
    tool_invocations: list[dict[str, Any]]        # ordered log for response/persistence

    # Evaluation outputs
    rule_results: list[dict[str, Any]]            # one entry per evaluated rule, in
                                                  # (shape canvas order, rule order)
    terminated_at_shape_id: str                   # shape that triggered TERMINATED_EARLY

    # Aggregation outputs
    final_decision_type: str
    applied_codes: list[str]
    narrative: str

    # Control / persistence
    status: str                                   # RUNNING|COMPLETED|FAILED|TERMINATED_EARLY
    error_message: str
    stages: list[dict[str, Any]]                  # per-node telemetry
    response: dict[str, Any]                      # final dict returned to caller
