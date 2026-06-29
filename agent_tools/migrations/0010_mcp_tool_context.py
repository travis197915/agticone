"""Add McpToolContext — LLM-derived context store for an MCP tool's response.

Hand-written (not via makemigrations) to keep the change targeted and avoid
pulling unrelated model drift into the migration graph.
"""
from __future__ import annotations

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agent_tools", "0009_drop_inprocess_tools"),
    ]

    operations = [
        migrations.CreateModel(
            name="McpToolContext",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("mcp_path", models.CharField(blank=True, default="", max_length=512)),
                ("summary", models.TextField(blank=True, default="")),
                ("fields", models.JSONField(blank=True, default=list)),
                ("sample_response", models.JSONField(blank=True, default=dict)),
                ("record_count", models.IntegerField(blank=True, null=True)),
                ("truncated", models.BooleanField(default=False)),
                ("llm_provider", models.CharField(blank=True, default="", max_length=32)),
                ("llm_model", models.CharField(blank=True, default="", max_length=64)),
                ("analyzed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "server",
                    models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="tool_contexts",
                        to="agent_tools.mcpserverconfig",
                    ),
                ),
                (
                    "tool",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mcp_context",
                        to="agent_tools.tool",
                    ),
                ),
            ],
            options={
                "db_table": "mcp_tool_context",
                "ordering": ["-analyzed_at", "-updated_at"],
            },
        ),
    ]
