"""
Registry of every LangChain tool ported under :mod:`agent_tools.tools`.

Two responsibilities:

* :func:`iter_tools` — lazy iterator that yields fully-built
  :class:`langchain_core.tools.StructuredTool` instances. Used by both
  the LangGraph invoke surface and the sync_to_db helper.
* :func:`sync_to_db` — idempotent upsert of one :class:`Tool` row per
  LangChain tool. Called from the seed data migration and from the
  ``sync_tool_registry`` management command.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


# Each entry is ``(module_path, builder_name, returns_list)``. We import
# lazily so a single broken import doesn't prevent the rest of the
# registry from loading.
_TOOL_FACTORIES: list[tuple[str, str, bool]] = [
    ("agent_tools.tools.doc360_tool",                       "build_tool",  False),
    ("agent_tools.tools.facets_tool",                       "build_tools", True),
    ("agent_tools.tools.facet_extension_portal_tool",       "build_tools", True),
    ("agent_tools.tools.cbd_tool",                          "build_tool",  False),
    ("agent_tools.tools.diagnosis_tool",                    "build_tool",  False),
    ("agent_tools.tools.linx_tool",                         "build_tool",  False),
    ("agent_tools.tools.opt_out_tool",                      "build_tool",  False),
    ("agent_tools.tools.cross_prevalence_billing_tool",     "build_tool",  False),
    ("agent_tools.tools.sop_step_persistence_tool",         "build_tool",  False),
    ("agent_tools.tools.llm_claim_parser",                  "build_tool",  False),
    ("agent_tools.tools.electronic_claim_parser",           "build_tool",  False),
]


def _import(module_path: str, attr: str) -> Callable[..., Any] | None:
    try:
        module = __import__(module_path, fromlist=[attr])
    except Exception as exc:
        logger.warning("agent_tools: failed to import %s (%s)", module_path, exc)
        return None
    return getattr(module, attr, None)


def iter_tools() -> Iterable[Any]:
    """Yield every :class:`StructuredTool` in the registry, in seed order."""
    for module_path, builder, returns_list in _TOOL_FACTORIES:
        builder_fn = _import(module_path, builder)
        if builder_fn is None:
            continue
        try:
            value = builder_fn()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("agent_tools: builder %s.%s raised: %s", module_path, builder, exc)
            continue
        if returns_list:
            for tool in value:
                yield tool
        else:
            yield value


def get_tool(name: str):
    """Return the :class:`StructuredTool` whose ``.name`` matches ``name``."""
    for tool in iter_tools():
        if tool.name == name:
            return tool
    return None


def _tool_args_schema(tool: Any) -> dict[str, Any]:
    """Return the Pydantic JSON Schema (v1 + v2 safe)."""
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return {}
    if hasattr(schema, "model_json_schema"):
        try:
            return schema.model_json_schema()
        except Exception:
            return {}
    if hasattr(schema, "schema"):
        try:
            return schema.schema()
        except Exception:
            return {}
    return {}


def _invoke_url_for(name: str) -> str:
    return f"/api/agent-tools/{name}/invoke"


def sync_to_db(*, apps=None) -> int:
    """Upsert one :class:`Tool` row per LangChain tool. Returns the row count.

    ``apps`` is the historical Apps registry passed during data migrations;
    when not provided we fall back to ``django.apps.apps`` for use from the
    management command.
    """
    if apps is None:
        from django.apps import apps as _apps
        Tool = _apps.get_model("agent_tools", "Tool")
    else:
        Tool = apps.get_model("agent_tools", "Tool")

    count = 0
    for tool in iter_tools():
        name = tool.name
        defaults = {
            "display_name": name.replace("_", " ").title(),
            "description": (tool.description or "").strip(),
            "kind": "langchain",
            "invoke_url": _invoke_url_for(name),
            "args_schema": _tool_args_schema(tool),
            "metadata": {},
            "is_active": True,
        }
        Tool.objects.update_or_create(name=name, defaults=defaults)
        count += 1
    logger.info("agent_tools: seeded %s tool rows", count)
    return count
