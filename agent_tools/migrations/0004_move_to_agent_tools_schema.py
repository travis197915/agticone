"""
Move the three agent_tools tables into the dedicated ``agent_tools``
Postgres schema.

Why a separate migration (not inlined into 0001)?

* 0001 created the tables under ``public.agent_tools_*`` and may already
  be applied in dev/staging databases. Changing 0001's ``options``
  would NOT re-issue DDL on those environments — the tables would stay
  in ``public`` while Django thinks they live in ``agent_tools``.
* This forward migration is idempotent: on a fresh install (where the
  legacy ``public.agent_tools_*`` names exist because 0001 just ran), it
  performs ``SET SCHEMA`` + ``RENAME``. On an install that already
  reflects the new layout, every step short-circuits to a no-op.

After this migration runs the layout is::

    agent_tools.tool
    agent_tools.node_rule_binding
    agent_tools.node_tool_binding

Django's model state is updated via :class:`SeparateDatabaseAndState` so
``model._meta.db_table`` and the actual table name stay in sync.
"""
from __future__ import annotations

from django.db import migrations


SCHEMA = "agent_tools"

# (legacy public name, new bare name inside the agent_tools schema, model name).
_TABLES: list[tuple[str, str, str]] = [
    ("agent_tools_tool",                "tool",                "tool"),
    ("agent_tools_node_rule_binding",   "node_rule_binding",   "noderulebinding"),
    ("agent_tools_node_tool_binding",   "node_tool_binding",   "nodetoolbinding"),
]


def _move_tables(apps, schema_editor):
    """ALTER ... SET SCHEMA + RENAME, idempotent across re-runs."""
    if schema_editor.connection.vendor != "postgresql":
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}";')

        for legacy, new_name, _model in _TABLES:
            # Where does the table live right now?
            cursor.execute(
                """
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE (table_schema = 'public'  AND table_name = %s)
                   OR (table_schema = %s       AND table_name = %s)
                   OR (table_schema = %s       AND table_name = %s)
                """,
                [legacy, SCHEMA, legacy, SCHEMA, new_name],
            )
            rows = cursor.fetchall()
            present = {(s, t) for s, t in rows}

            if (SCHEMA, new_name) in present:
                continue

            if ("public", legacy) in present:
                cursor.execute(
                    f'ALTER TABLE public."{legacy}" SET SCHEMA "{SCHEMA}";'
                )
                cursor.execute(
                    f'ALTER TABLE "{SCHEMA}"."{legacy}" '
                    f'RENAME TO "{new_name}";'
                )
                continue

            if (SCHEMA, legacy) in present:
                cursor.execute(
                    f'ALTER TABLE "{SCHEMA}"."{legacy}" '
                    f'RENAME TO "{new_name}";'
                )
                continue


def _unmove_tables(apps, schema_editor):
    """Reverse: put tables back under public.agent_tools_* (rare/dev-only)."""
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for legacy, new_name, _model in _TABLES:
            cursor.execute(
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
                """,
                [SCHEMA, new_name],
            )
            if not cursor.fetchone():
                continue
            cursor.execute(
                f'ALTER TABLE "{SCHEMA}"."{new_name}" RENAME TO "{legacy}";'
            )
            cursor.execute(
                f'ALTER TABLE "{SCHEMA}"."{legacy}" SET SCHEMA public;'
            )


class Migration(migrations.Migration):

    dependencies = [
        ("agent_tools", "0003_migrate_shape_properties"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(_move_tables, _unmove_tables),
            ],
            state_operations=[
                # The runtime model.Meta.db_table is the bare name; the
                # connection's search_path (public,agent_tools) resolves
                # the unqualified reference to the schema-qualified
                # table the database operations above moved into place.
                migrations.AlterModelTable(name="tool",            table="tool"),
                migrations.AlterModelTable(name="noderulebinding", table="node_rule_binding"),
                migrations.AlterModelTable(name="nodetoolbinding", table="node_tool_binding"),
            ],
        ),
    ]
