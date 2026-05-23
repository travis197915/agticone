"""
Initial migration for the agent_tools registry + bindings.

Creates three tables:

* ``agent_tools_tool``               — :class:`agent_tools.models.Tool`
* ``agent_tools_node_rule_binding``  — :class:`agent_tools.models.NodeRuleBinding`
* ``agent_tools_node_tool_binding``  — :class:`agent_tools.models.NodeToolBinding`

We depend on the latest ``builder`` and ``sop_ingestion`` migrations so the
referenced FK targets exist when this migration runs.
"""
from __future__ import annotations

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("agent_tools", "0000_create_schema"),
        ("builder", "0001_initial"),
        ("sop_ingestion", "0010_sop_exclusion_html_block"),
    ]

    operations = [
        migrations.CreateModel(
            name="Tool",
            fields=[
                ("id", models.UUIDField(
                    default=uuid.uuid4, editable=False,
                    primary_key=True, serialize=False,
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.SlugField(max_length=128, unique=True)),
                ("display_name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True, default="")),
                ("kind", models.CharField(
                    choices=[
                        ("langchain", "LangChain tool"),
                        ("api_agent", "Runtime API agent"),
                    ],
                    db_index=True,
                    default="langchain",
                    max_length=16,
                )),
                ("invoke_url", models.CharField(blank=True, default="", max_length=2048)),
                ("args_schema", models.JSONField(blank=True, default=dict)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("endpoint_id", models.CharField(
                    blank=True, db_index=True, default="", max_length=128,
                )),
                ("is_active", models.BooleanField(db_index=True, default=True)),
            ],
            options={
                "db_table": "agent_tools_tool",
                "ordering": ["display_name", "name"],
            },
        ),
        migrations.AddIndex(
            model_name="tool",
            index=models.Index(
                fields=["kind", "is_active"],
                name="agent_tools_tool_kind_act_idx",
            ),
        ),
        migrations.CreateModel(
            name="NodeRuleBinding",
            fields=[
                ("id", models.UUIDField(
                    default=uuid.uuid4, editable=False,
                    primary_key=True, serialize=False,
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("rule_key", models.CharField(db_index=True, max_length=255)),
                ("condition", models.TextField(blank=True, default="")),
                ("action", models.TextField(blank=True, default="")),
                ("references_json", models.JSONField(blank=True, default=list)),
                ("excluded_by_json", models.JSONField(blank=True, default=list)),
                ("html_reference_json", models.JSONField(blank=True, default=dict)),
                ("ordering", models.PositiveIntegerField(default=0)),
                ("shape", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="rule_bindings",
                    to="builder.shape",
                )),
                ("sop", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="rule_bindings",
                    to="sop_ingestion.auditsop",
                )),
            ],
            options={
                "db_table": "agent_tools_node_rule_binding",
                "ordering": ["shape", "ordering", "created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="noderulebinding",
            constraint=models.UniqueConstraint(
                fields=("shape", "rule_key"),
                name="uniq_node_rule_binding_shape_rule",
            ),
        ),
        migrations.AddIndex(
            model_name="noderulebinding",
            index=models.Index(
                fields=["shape", "ordering"],
                name="agent_tools_nrb_shape_ord_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="noderulebinding",
            index=models.Index(
                fields=["sop", "rule_key"],
                name="agent_tools_nrb_sop_key_idx",
            ),
        ),
        migrations.CreateModel(
            name="NodeToolBinding",
            fields=[
                ("id", models.UUIDField(
                    default=uuid.uuid4, editable=False,
                    primary_key=True, serialize=False,
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("args_template", models.JSONField(blank=True, default=dict)),
                ("ordering", models.PositiveIntegerField(default=0)),
                ("shape", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="tool_bindings",
                    to="builder.shape",
                )),
                ("tool", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="bindings",
                    to="agent_tools.tool",
                )),
                ("rule_binding", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="tool_bindings",
                    to="agent_tools.noderulebinding",
                )),
            ],
            options={
                "db_table": "agent_tools_node_tool_binding",
                "ordering": ["shape", "ordering", "created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="nodetoolbinding",
            constraint=models.UniqueConstraint(
                fields=("shape", "tool", "rule_binding"),
                name="uniq_node_tool_binding_shape_tool_rule",
            ),
        ),
        migrations.AddIndex(
            model_name="nodetoolbinding",
            index=models.Index(
                fields=["shape", "ordering"],
                name="agent_tools_ntb_shape_ord_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="nodetoolbinding",
            index=models.Index(
                fields=["tool"],
                name="agent_tools_ntb_tool_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="nodetoolbinding",
            index=models.Index(
                fields=["rule_binding"],
                name="agent_tools_ntb_rb_idx",
            ),
        ),
    ]
