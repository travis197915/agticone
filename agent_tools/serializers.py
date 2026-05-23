"""DRF serializers for the agent_tools surface."""
from __future__ import annotations

from rest_framework import serializers

from .models import NodeRuleBinding, NodeToolBinding, Tool


class ToolSerializer(serializers.ModelSerializer):
    """Public registry shape consumed by the SPA + agents."""

    # Aliases the SPA expects.
    tool_kind = serializers.CharField(source="kind", read_only=True)

    class Meta:
        model = Tool
        fields = [
            "id",
            "name",
            "display_name",
            "description",
            "kind",
            "tool_kind",
            "invoke_url",
            "args_schema",
            "metadata",
            "endpoint_id",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class NodeRuleBindingSerializer(serializers.ModelSerializer):
    class Meta:
        model = NodeRuleBinding
        fields = [
            "id",
            "shape",
            "sop",
            "rule_key",
            "condition",
            "action",
            "references_json",
            "excluded_by_json",
            "html_reference_json",
            "ordering",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class NodeToolBindingSerializer(serializers.ModelSerializer):
    tool_name = serializers.CharField(source="tool.name", read_only=True)
    tool_display_name = serializers.CharField(source="tool.display_name", read_only=True)
    tool_kind = serializers.CharField(source="tool.kind", read_only=True)

    class Meta:
        model = NodeToolBinding
        fields = [
            "id",
            "shape",
            "tool",
            "tool_name",
            "tool_display_name",
            "tool_kind",
            "args_template",
            "rule_binding",
            "ordering",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id", "tool_name", "tool_display_name", "tool_kind",
            "created_at", "updated_at",
        ]
