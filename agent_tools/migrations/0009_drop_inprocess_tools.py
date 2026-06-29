"""
Data migration: remove the auto-seeded in-process LangChain tool rows so the
Tool Calls config surfaces only the MCP-routed tools an operator configures.

These rows are a DB mirror of the in-process tools in
``agent_tools.registry._TOOL_FACTORIES``. The execution engine resolves tools
by name from that code registry (see ``uhc_execution_engine.tool_runner``), so
deleting the rows does NOT affect runtime tool execution — it only clears them
from the config / attach UI. We delete only ``langchain`` rows that have no
``mcp_path`` (i.e. pure in-process tools); any tool that has been pointed at an
MCP route is preserved.

Reverse re-seeds from the registry.
"""
from __future__ import annotations

from django.db import migrations


def _drop_inprocess(apps, _schema_editor):
    Tool = apps.get_model("agent_tools", "Tool")
    for tool in Tool.objects.filter(kind="langchain"):
        meta = tool.metadata if isinstance(tool.metadata, dict) else {}
        if not meta.get("mcp_path"):
            tool.delete()


def _reseed(_apps, _schema_editor):
    from agent_tools.registry import sync_to_db
    sync_to_db()


class Migration(migrations.Migration):

    dependencies = [
        ("agent_tools", "0009_merge_20260626_1215"),
    ]

    operations = [
        migrations.RunPython(_drop_inprocess, _reseed),
    ]
