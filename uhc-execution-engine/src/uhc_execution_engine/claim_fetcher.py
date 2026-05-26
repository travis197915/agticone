"""Outer-layer claim fetch + optional parse.

For v1 the fetcher tool is hard-wired to ``linx_claim_search``; the parse step
is opt-in based on whether the workflow has ``llm_parse_claim_with_ontology``
bound to any shape. Both go through :func:`tool_runner.invoke_tool`.
"""
from __future__ import annotations

import logging
from typing import Any

from .tool_runner import invoke_tool

logger = logging.getLogger(__name__)

FETCH_TOOL = "linx_claim_search"
PARSE_TOOL = "llm_parse_claim_with_ontology"


def fetch_claim(claim_id: str, *, extra_args: dict[str, Any] | None = None
                ) -> dict[str, Any]:
    """Call ``linx_claim_search`` for one claim id.

    The Excel column value is fed in as ``subscriber_id`` (the only required
    field on the tool's schema). Callers may supply ``extra_args`` (e.g.
    date range) to merge into the request.
    """
    args: dict[str, Any] = {"subscriber_id": claim_id}
    if extra_args:
        args.update({k: v for k, v in extra_args.items() if v is not None})
    return invoke_tool(FETCH_TOOL, args)


def parse_claim(raw_fetch: dict[str, Any], template_name: str = "default"
                ) -> dict[str, Any]:
    """Normalize the linx payload via ``llm_parse_claim_with_ontology``."""
    args = {"claim_data": raw_fetch, "template_name": template_name}
    return invoke_tool(PARSE_TOOL, args)


def workflow_uses_parser(all_tool_bindings: list[dict[str, Any]]) -> bool:
    return any(tb["tool_name"] == PARSE_TOOL for tb in all_tool_bindings)
