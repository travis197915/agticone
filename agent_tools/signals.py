"""Signals that keep the execution engine's in-process config cache fresh.

When a :class:`~agent_tools.models.SopFieldMapping` or
:class:`~agent_tools.models.ClaimOntologyField` row is saved or deleted (e.g.
from the builder UI), drop the engine's cached copy in *this* process so edits
take effect immediately. Other processes (Celery workers) pick the change up via
the cheap (count, max-updated) watermark in ``field_mapping._load``.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import ClaimOntologyField, McpServerConfig, SopFieldMapping

logger = logging.getLogger(__name__)


def _invalidate_engine_cache() -> None:
    try:
        from uhc_execution_engine.field_mapping import reset_cache
    except Exception:  # pragma: no cover - engine not importable in some contexts
        return
    reset_cache()


@receiver(post_save, sender=SopFieldMapping)
@receiver(post_delete, sender=SopFieldMapping)
@receiver(post_save, sender=ClaimOntologyField)
@receiver(post_delete, sender=ClaimOntologyField)
def _on_config_change(sender, **kwargs) -> None:
    _invalidate_engine_cache()


def _invalidate_mcp_cache() -> None:
    """Drop the engine's cached active MCP server config so UI edits (base URL,
    auth, activation toggle) take effect on the next tool call in this process."""
    try:
        from uhc_execution_engine.mcp_client import reset_cache
    except Exception:  # pragma: no cover - engine not importable in some contexts
        return
    reset_cache()


@receiver(post_save, sender=McpServerConfig)
@receiver(post_delete, sender=McpServerConfig)
def _on_mcp_config_change(sender, **kwargs) -> None:
    _invalidate_mcp_cache()
