"""
Data migration: seed one :class:`agent_tools.Tool` row per LangChain tool.
"""
from __future__ import annotations

from django.db import migrations


def _seed(apps, _schema_editor):
    # Import lazily so this migration doesn't import langchain at module
    # load time (faster ``makemigrations`` parsing).
    from agent_tools.registry import sync_to_db
    sync_to_db(apps=apps)


def _noop(_apps, _schema_editor):
    # Reverse migration intentionally leaves rows in place — the registry
    # is rebuilt on every ``sync_to_db()`` call anyway.
    return


class Migration(migrations.Migration):

    dependencies = [
        ("agent_tools", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(_seed, _noop),
    ]
