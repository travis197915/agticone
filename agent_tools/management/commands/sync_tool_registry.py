"""
``manage.py sync_tool_registry`` — re-run the seed without a migration.

Useful after editing tool descriptions, input schemas, or adding new
tools to the LangChain catalog.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Synchronize the agent_tools.Tool table with the LangChain catalog."

    def handle(self, *args, **options):
        from agent_tools.registry import sync_to_db
        count = sync_to_db()
        self.stdout.write(self.style.SUCCESS(
            f"agent_tools: synchronized {count} tool rows"
        ))
