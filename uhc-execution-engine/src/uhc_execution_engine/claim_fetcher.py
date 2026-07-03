"""Outer-layer claim fetch + optional parse.

A **fetch tool must be configured** before a batch can run — there is no
silent default. Resolution order:

1. ``Workflow.metadata['fetch_tool']`` when set explicitly.
2. The first canvas tool binding whose name is in :data:`FETCH_TOOLS`.

Supported fetch tools:

* ``facets_get_claim_summary`` — ``claim_number=claim_id``
* ``facets_get_summary``        — ``claim_number=claim_id``
* ``linx_claim_search``         — ``subscriber_id=claim_id``

The parse step (``llm_parse_claim_with_ontology``) remains opt-in: it runs only
when that tool is bound on the workflow. Both fetch and parse go through
:func:`tool_runner.invoke_tool`.
"""
from __future__ import annotations

import logging
from typing import Any

from .tool_runner import invoke_tool

logger = logging.getLogger(__name__)

FETCH_TOOL = "linx_claim_search"
PARSE_TOOL = "llm_parse_claim_with_ontology"

# Tools that may fetch the claim payload in the outer (batch) layer.
FETCH_TOOLS = frozenset({
    "linx_claim_search",
    "facets_get_claim_summary",
    "facets_get_summary",
})

TOOLS_NOT_ADDED_ERROR = (
    "no tools attached to this workflow — add tool calls on the canvas "
    "(including a claim fetch tool such as facets_get_summary) before running claims"
)

NO_FETCH_TOOL_ERROR = (
    "no claim fetch tool attached — bind facets_get_summary, facets_get_claim_summary, "
    "or linx_claim_search to a shape, or set Workflow.metadata['fetch_tool']"
)

# Tools that take claim_number instead of subscriber_id.
_CLAIM_NUMBER_TOOLS = {"facets_get_claim_summary", "facets_get_summary"}


def _fetch_args(tool_name: str, claim_id: str,
                extra_args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the argument dict appropriate for the chosen fetch tool."""
    if tool_name in _CLAIM_NUMBER_TOOLS:
        args: dict[str, Any] = {"claim_number": claim_id}
    else:
        args = {"subscriber_id": claim_id}
    if extra_args:
        args.update({k: v for k, v in extra_args.items() if v is not None})
    return args


def resolve_fetch_tool(
    workflow_id: str,
    all_tool_bindings: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return the fetch tool for this workflow, or ``None`` when not configured."""
    try:
        from builder.models import Workflow
        wf = Workflow.objects.filter(id=workflow_id).only("metadata").first()
        if wf:
            meta_tool = (wf.metadata or {}).get("fetch_tool")
            if meta_tool:
                return str(meta_tool).strip()
    except Exception as exc:  # pragma: no cover
        logger.warning("resolve_fetch_tool: could not read workflow metadata (%s)", exc)

    for tb in all_tool_bindings or []:
        name = tb.get("tool_name")
        if name in FETCH_TOOLS:
            return name
    return None


def fetch_tool_error(
    all_tool_bindings: list[dict[str, Any]] | None = None,
) -> str:
    """Human-readable reason fetch cannot proceed (for batch / run errors)."""
    if not all_tool_bindings:
        return TOOLS_NOT_ADDED_ERROR
    return NO_FETCH_TOOL_ERROR


def fetch_claim(claim_id: str, *,
                tool_name: str | None = None,
                extra_args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fetch a single claim via the nominated tool.

    ``tool_name`` must be set — use :func:`resolve_fetch_tool` first.
    """
    if not tool_name:
        return {
            "ok": False, "tool": "", "args": {"claim_id": claim_id},
            "result": None, "error": NO_FETCH_TOOL_ERROR,
            "duration_ms": 0,
        }
    args = _fetch_args(tool_name, claim_id, extra_args)
    return invoke_tool(tool_name, args)


def workflow_fetch_tool(workflow_id: str) -> str | None:
    """Legacy entrypoint — prefer :func:`resolve_fetch_tool` with bindings."""
    return resolve_fetch_tool(workflow_id, None)


def parse_claim(raw_fetch: dict[str, Any], template_name: str = "default"
                ) -> dict[str, Any]:
    """Normalize the fetch payload via ``llm_parse_claim_with_ontology``."""
    args = {"claim_data": raw_fetch, "template_name": template_name}
    return invoke_tool(PARSE_TOOL, args)


def workflow_uses_parser(all_tool_bindings: list[dict[str, Any]]) -> bool:
    return any(tb["tool_name"] == PARSE_TOOL for tb in all_tool_bindings)
