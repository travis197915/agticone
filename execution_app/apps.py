"""Django app for the rule execution agent.

Owns persistence for batch runs, per-claim runs, rule evaluations and tool
invocations. The actual pipeline lives in the editable
``uhc-execution-engine`` package and is invoked from this app's REST views.
"""
from __future__ import annotations

from django.apps import AppConfig


class ExecutionAgentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "execution_app"
    verbose_name = "Rule Execution Agent"
