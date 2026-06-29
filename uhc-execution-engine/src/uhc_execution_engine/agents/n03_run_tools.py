"""n03 — run_tools: execute every non-fetch tool binding for the workflow.

We've already fetched + parsed the claim in the outer layer, so we skip
``linx_claim_search`` and ``llm_parse_claim_with_ontology`` here to avoid
double work.
"""
from __future__ import annotations

import concurrent.futures as _cf
import contextvars
import json
import logging
import time
from typing import Any

from ..claim_fetcher import FETCH_TOOL, PARSE_TOOL
from ..config import get_config
from ..state import ExecutionState
from ..tool_runner import invoke_tool

logger = logging.getLogger(__name__)

_SKIP_TOOLS = {FETCH_TOOL, PARSE_TOOL}


def _dedup_key(tool_name: str, args: dict[str, Any]) -> str:
    """Stable identity for a tool call so identical (tool, args) run once."""
    try:
        return tool_name + "|" + json.dumps(args, sort_keys=True, default=str)
    except Exception:
        return tool_name + "|" + repr(sorted((args or {}).items(), key=lambda kv: str(kv[0])))


def _merge_args(template: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
    """Start from the binding's args_template, then fill in any obvious
    claim-derived defaults the tool is likely to want.
    """
    args: dict[str, Any] = dict(template or {})
    # Common claim fields tools may want; non-destructive (template wins).
    for key in ("subscriber_id", "member_id", "claim_id",
                "diagnosis_code", "cpt_code", "place_of_service",
                "first_name", "last_name", "dob"):
        if key in claim and key not in args:
            args[key] = claim[key]
    # ``claim_number`` is the canonical claim identifier several in-process tools
    # require (e.g. facet_ext_portal_* which resolve PRPR_ID from it). MCP-routed
    # tools ignore extra args, so this is safe to always supply as an alias of
    # the claim id when not already present.
    if "claim_number" not in args:
        cn = claim.get("claim_number") or claim.get("claim_id")
        if cn:
            args["claim_number"] = cn
    return args


def run_tools(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    if state.get("status") == "FAILED":
        return {}

    cfg = get_config()
    claim = state.get("claim") or {}
    invocations = list(state.get("tool_invocations") or [])
    results_by_binding: dict[str, dict[str, Any]] = dict(state.get("tool_results") or {})

    # Lazy mode: defer EVALUATE-phase tool invocation to execute_shapes, which
    # only runs the tools for steps the router actually reaches. Pre-seeded
    # results (injected/cached) are preserved and reused as before.
    if cfg.lazy_tools:
        stages.append({"node": "run_tools", "status": "OK",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": "deferred to execute_shapes (lazy_tools)"})
        return {
            "tool_invocations": invocations,
            "tool_results": results_by_binding,
            "stages": stages,
        }

    # Collect every unique tool binding from both scoping maps (tools_by_shape
    # is a superset of tools_by_rule_key, so it covers rule-scoped tools too).
    seen: set[str] = set()
    bindings: list[dict[str, Any]] = []
    for tb_list in (state.get("tools_by_shape") or {}).values():
        for tb in tb_list:
            if tb["binding_id"] in seen:
                continue
            if tb["tool_name"] in _SKIP_TOOLS:
                continue
            # Reuse a pre-seeded result (e.g. injected/cached) instead of
            # re-invoking the tool live. Backward compatible: empty seed map
            # means every binding is invoked as before.
            if tb["binding_id"] in results_by_binding:
                continue
            seen.add(tb["binding_id"])
            bindings.append(tb)

    # Dedup identical (tool_name, resolved args) so the same call runs ONCE and
    # its result fans out to every binding that shares it — this is what makes
    # "do all tool calls before execution" cheap when many rules bind the same
    # tool with the same args. ``groups`` maps a dedup key -> list of bindings.
    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for tb in bindings:
        args = _merge_args(tb["args_template"], claim)
        groups.setdefault(_dedup_key(tb["tool_name"], args), []).append((tb, args))

    def _run_group(rep_tool: str, rep_args: dict[str, Any]) -> dict[str, Any]:
        return invoke_tool(rep_tool, rep_args)

    # Invoke the distinct calls in parallel. Propagate the contextvars (run_id /
    # batch_id) so LLMCallLog stamping + SSE publishing keep working in workers.
    # NOTE: each task gets its OWN copied Context — a single Context object
    # cannot be entered by more than one thread at a time (RuntimeError), which
    # would otherwise serialize everything.
    workers = max(1, min(int(getattr(cfg, "tool_prefetch_workers", 8) or 8), len(groups) or 1))
    outcomes: dict[str, dict[str, Any]] = {}
    if groups:
        if workers > 1 and len(groups) > 1:
            with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {}
                for key, members in groups.items():
                    rep_tb, rep_args = members[0]
                    task_ctx = contextvars.copy_context()
                    futs[ex.submit(task_ctx.run, _run_group, rep_tb["tool_name"], rep_args)] = key
                for fut in _cf.as_completed(futs):
                    key = futs[fut]
                    try:
                        outcomes[key] = fut.result()
                    except Exception as exc:  # pragma: no cover — defensive
                        rep_tb, rep_args = groups[key][0]
                        outcomes[key] = {"ok": False, "tool": rep_tb["tool_name"],
                                         "args": rep_args, "result": None,
                                         "error": str(exc), "duration_ms": 0}
        else:
            for key, members in groups.items():
                rep_tb, rep_args = members[0]
                outcomes[key] = _run_group(rep_tb["tool_name"], rep_args)

    # Fan each distinct outcome out to every binding that shared the call.
    for key, members in groups.items():
        out = outcomes.get(key) or {"ok": False, "result": None, "error": "no result", "duration_ms": 0}
        for tb, args in members:
            record = {
                "binding_id": tb["binding_id"],
                "tool_name": tb["tool_name"],
                "phase": "EVALUATE",
                "args": out.get("args") or args,
                "ok": out["ok"],
                "result": out["result"],
                "error": out["error"],
                "duration_ms": out["duration_ms"],
            }
            invocations.append(record)
            results_by_binding[tb["binding_id"]] = record

    stages.append({"node": "run_tools", "status": "OK",
                   "ms": int((time.time() - t0) * 1000),
                   "msg": f"invoked {len(groups)} distinct call(s) "
                          f"across {len(bindings)} binding(s) [parallel]"})
    logger.info("run_tools claim=%s prefetched %d distinct tool call(s) for %d binding(s)",
                state.get("claim_id") or "-", len(groups), len(bindings))
    return {
        "tool_invocations": invocations,
        "tool_results": results_by_binding,
        "stages": stages,
    }
