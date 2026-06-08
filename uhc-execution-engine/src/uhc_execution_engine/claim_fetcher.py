"""Outer-layer claim fetch + optional parse.

The fetcher tool defaults to ``linx_claim_search`` but can be overridden
per-workflow via ``Workflow.metadata["fetch_tool"]``.  Supported values:

* ``"linx_claim_search"`` (default) — passes ``subscriber_id=claim_id``
* ``"facets_get_claim_summary"`` — passes ``claim_number=claim_id``
* ``"facets_get_summary"``        — passes ``claim_number=claim_id``

The parse step (``llm_parse_claim_with_ontology``) is opt-in based on whether
the workflow has that tool bound to any shape.  Both go through
:func:`tool_runner.invoke_tool`.
"""
from __future__ import annotations

import logging
from typing import Any

from .tool_runner import invoke_tool

logger = logging.getLogger(__name__)

FETCH_TOOL = "linx_claim_search"
PARSE_TOOL = "llm_parse_claim_with_ontology"

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


def fetch_claim(claim_id: str, *,
                tool_name: str | None = None,
                extra_args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fetch a single claim via the nominated tool.

    ``tool_name`` overrides the default ``linx_claim_search``; pass the value
    from ``Workflow.metadata['fetch_tool']`` to switch per-workflow.
    """
    tool = tool_name or FETCH_TOOL
    args = _fetch_args(tool, claim_id, extra_args)
    return invoke_tool(tool, args)


def workflow_fetch_tool(workflow_id: str) -> str:
    """Return the fetch tool configured for a workflow, falling back to linx.

    Reads ``Workflow.metadata['fetch_tool']`` via the Django ORM.  Safe to
    call in any context where Django is set up; returns ``FETCH_TOOL`` if the
    workflow is not found or has no preference set.
    """
    try:
        from builder.models import Workflow
        wf = Workflow.objects.filter(id=workflow_id).only("metadata").first()
        if wf:
            tool = (wf.metadata or {}).get("fetch_tool") or FETCH_TOOL
            return tool
    except Exception as exc:  # pragma: no cover
        logger.warning("workflow_fetch_tool: could not read metadata (%s)", exc)
    return FETCH_TOOL


def parse_claim(raw_fetch: dict[str, Any], template_name: str = "default"
                ) -> dict[str, Any]:
    """Normalize the fetch payload via ``llm_parse_claim_with_ontology``."""
    args = {"claim_data": raw_fetch, "template_name": template_name}
    return invoke_tool(PARSE_TOOL, args)


def workflow_uses_parser(all_tool_bindings: list[dict[str, Any]]) -> bool:
    return any(tb["tool_name"] == PARSE_TOOL for tb in all_tool_bindings)
