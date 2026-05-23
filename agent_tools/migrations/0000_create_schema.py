"""
Create the dedicated Postgres ``agent_tools`` schema.

Everything in this app — Tool registry, NodeRuleBinding,
NodeToolBinding, plus the seeded LangChain tool rows — lives under this
schema rather than ``public``. That gives us:

* one-shot drop/clean ("DROP SCHEMA agent_tools CASCADE") for dev resets
  without touching the rest of the database,
* clean grant boundaries for ops if/when this app's tables get a tighter
  read-only / read-write role,
* and a literal namespace that mirrors the Django app boundary.

The migration is a no-op on non-Postgres backends (sqlite test runners
etc.). All subsequent migrations in this app declare a dependency on
this one so the schema is guaranteed to exist before any CREATE TABLE.
"""
from __future__ import annotations

from django.db import migrations


SCHEMA_NAME = "agent_tools"


def _create_schema(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}";')


def _drop_schema(apps, schema_editor):
    """Reverse migration — drop the schema and everything in it."""
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA_NAME}" CASCADE;')


class Migration(migrations.Migration):

    initial = True
    # No dependencies — this needs to run before agent_tools.0001_initial
    # so the schema exists before the CreateModel ops below execute their
    # CREATE TABLE statements against ``"agent_tools"."..."``.
    dependencies: list = []

    operations = [
        migrations.RunPython(_create_schema, _drop_schema),
    ]
