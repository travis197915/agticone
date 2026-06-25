"""
Django app config for ``agent_tools``.

The app owns three concerns:

* The DB-backed :class:`~agent_tools.models.Tool` registry — one row per
  LangChain tool ported under :mod:`agent_tools.tools` plus one row per
  registered runtime HTTP agent.
* :class:`~agent_tools.models.NodeRuleBinding` and
  :class:`~agent_tools.models.NodeToolBinding` — the relational replacement
  for the old ``Shape.properties.{sop_rules,tool_calls}`` JSON blobs.
* The mock upstream server mounted under ``/api/mocks/`` and the
  LangGraph-wrapped invoke surface under ``/api/agent-tools/``.

``ready()`` loads ``agent_tools/.env.tools`` into the process environment
without overriding values from the main project ``.env``.
"""
from __future__ import annotations

from django.apps import AppConfig


class AgentToolsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "agent_tools"
    verbose_name = "Agent Tools (LangChain + LangGraph)"

    def ready(self) -> None:
        # Load .env.tools without overriding values that are already in os.environ.
        from . import env as _env
        _env.load_env_tools()
        # Register signals that invalidate the engine's field-mapping/ontology
        # cache when those config rows are edited from the UI.
        from . import signals  # noqa: F401
