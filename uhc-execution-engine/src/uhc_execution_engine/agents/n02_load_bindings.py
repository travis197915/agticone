"""n02 — load_bindings: hydrate rules + tool bindings for the workflow."""
from __future__ import annotations

import logging
import time

from ..rule_loader import load_workflow_bindings
from ..state import ExecutionState

logger = logging.getLogger(__name__)


def load_bindings(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    if state.get("status") == "FAILED":
        return {}

    try:
        loaded = load_workflow_bindings(state["workflow_id"])
    except Exception as exc:
        stages.append({"node": "load_bindings", "status": "FAIL",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": str(exc)})
        return {"status": "FAILED", "error_message": f"load_bindings: {exc}",
                "stages": stages}

    pre = loaded["preconditions"]
    dec = loaded["decisions"]
    shapes = loaded["shapes"]
    shapes_with_rules = sum(1 for s in shapes if s.get("rules"))
    n_tools = len(loaded["all_tool_bindings"])

    # Pre-execution breadcrumb. INFO so it shows for every claim — this is
    # the single most useful line when diagnosing "the engine ran but did
    # nothing": it tells you exactly what the loader produced before any
    # downstream node has a chance to silently skip an empty list.
    logger.info(
        "load_bindings workflow=%s claim=%s shapes=%d with_rules=%d pre=%d dec=%d tools=%d",
        state.get("workflow_id"), state.get("claim_id") or "-",
        len(shapes), shapes_with_rules, len(pre), len(dec), n_tools,
    )

    # The v2 evaluator iterates `shapes`. A workflow is "rule-less" only
    # when every shape grouping is empty and the legacy flat lists are too.
    has_any_rule = any(s.get("rules") for s in shapes) or bool(pre) or bool(dec)
    if not has_any_rule:
        logger.warning(
            "load_bindings workflow=%s has zero rule bindings; aborting run as FAILED",
            state.get("workflow_id"),
        )
        stages.append({"node": "load_bindings", "status": "FAIL",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": "workflow has no attached rules"})
        return {"status": "FAILED",
                "error_message": "workflow has no attached rules",
                "stages": stages}

    # Loader accepted the workflow but the executor will see nothing to do.
    # This is the silent-failure case documented in EXECUTION_ENGINE.md §9.
    if shapes_with_rules == 0 and (pre or dec):
        logger.warning(
            "load_bindings workflow=%s has %d pre + %d dec on flat lists but no "
            "shape-attached rules; execute_shapes will be a no-op and the claim "
            "will fall through to default ALLOW. Likely a migration / rule_loader "
            "issue, not a real adjudication.",
            state.get("workflow_id"), len(pre), len(dec),
        )

    stages.append({"node": "load_bindings", "status": "OK",
                   "ms": int((time.time() - t0) * 1000),
                   "msg": f"{len(shapes)} shapes / {len(pre)} pre / {len(dec)} dec / "
                          f"{n_tools} tools"})
    return {
        "preconditions": pre,
        "decisions": dec,
        "shapes": shapes,
        "tools_by_rule_key": loaded["tools_by_rule_key"],
        "tools_by_shape": loaded["tools_by_shape"],
        "stages": stages,
    }
