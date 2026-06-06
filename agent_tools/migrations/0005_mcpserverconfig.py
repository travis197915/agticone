"""Add McpServerConfig — shared base endpoint for external claims MCP/REST server.

Hand-written (not via makemigrations) to keep the change targeted and avoid
pulling unrelated model drift into the migration graph.
"""
from __future__ import annotations

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agent_tools", "0004_move_to_agent_tools_schema"),
    ]

    operations = [
        migrations.CreateModel(
            name="McpServerConfig",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("label", models.CharField(default="claims-mock-mcp", max_length=128)),
                ("base_url", models.CharField(max_length=2048)),
                ("auth_header", models.CharField(default="x-api-key", max_length=64)),
                ("api_key", models.CharField(blank=True, default="", max_length=512)),
                ("http_method", models.CharField(default="POST", max_length=8)),
                ("claim_arg", models.CharField(default="claim_number", max_length=64)),
                ("timeout_seconds", models.PositiveIntegerField(default=30)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
            ],
            options={
                "db_table": "mcp_server_config",
                "ordering": ["-is_active", "-updated_at"],
            },
        ),
    ]
