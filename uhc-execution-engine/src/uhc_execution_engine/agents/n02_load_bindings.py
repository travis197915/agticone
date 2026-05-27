"""n02 — load_bindings: hydrate rules + tool bindings for the workflow."""
from __future__ import annotations

import time

from ..rule_loader import load_workflow_bindings
from ..state import ExecutionState


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
    # The v2 evaluator iterates `shapes`. A workflow is "rule-less" only
    # when every shape grouping is empty and the legacy flat lists are too.
    has_any_rule = any(s.get("rules") for s in shapes) or bool(pre) or bool(dec)
    if not has_any_rule:
        stages.append({"node": "load_bindings", "status": "FAIL",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": "workflow has no attached rules"})
        return {"status": "FAILED",
                "error_message": "workflow has no attached rules",
                "stages": stages}

    shapes = loaded["shapes"]
    stages.append({"node": "load_bindings", "status": "OK",
                   "ms": int((time.time() - t0) * 1000),
                   "msg": f"{len(shapes)} shapes / {len(pre)} pre / {len(dec)} dec / "
                          f"{len(loaded['all_tool_bindings'])} tools"})
    return {
        "preconditions": pre,
        "decisions": dec,
        "shapes": shapes,
        "tools_by_rule_key": loaded["tools_by_rule_key"],
        "tools_by_shape": loaded["tools_by_shape"],
        "stages": stages,
    }
