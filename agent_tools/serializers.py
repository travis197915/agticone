"""DRF serializers for the agent_tools surface."""
from __future__ import annotations

from rest_framework import serializers

from .models import (ClaimOntologyField, NodeRuleBinding, NodeToolBinding,
                     SopFieldMapping, Tool)

# Systems the resolver searches, in preference order, with friendly labels so
# the UI can explain what each cryptic source actually is.
SYSTEM_LABELS = [
    {"key": "FACETS", "label": "FACETS (system of record)"},
    {"key": "DOC360", "label": "DOC360 (claim image / CMS-1500)"},
    {"key": "CBS", "label": "CBS"},
    {"key": "CBD", "label": "CBD (coverage benefit data)"},
    {"key": "NPI", "label": "NPI registry"},
]


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


class SopFieldMappingSerializer(serializers.ModelSerializer):
    """Editable SOP business-field → source-system key mapping."""

    class Meta:
        model = SopFieldMapping
        fields = [
            "id",
            "sop_field",
            "description",
            "category",
            "systems",
            "notes",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_systems(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("systems must be an object keyed by source system.")
        valid = {s["key"] for s in SYSTEM_LABELS}
        cleaned: dict[str, list[str]] = {}
        for k, v in value.items():
            if k not in valid:
                raise serializers.ValidationError(f"unknown source system '{k}'.")
            if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                raise serializers.ValidationError(f"'{k}' must be a list of strings.")
            cleaned[k] = v
        # Always present all systems so the UI renders a complete grid.
        for s in valid:
            cleaned.setdefault(s, [])
        return cleaned


class ClaimOntologyFieldSerializer(serializers.ModelSerializer):
    """Editable CMS-1500 ontology row (canonical field + raw-label aliases)."""

    class Meta:
        model = ClaimOntologyField
        fields = [
            "id",
            "namespace",
            "canonical_field",
            "description",
            "aliases",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_aliases(self, value):
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            raise serializers.ValidationError("aliases must be a list of strings.")
        return value


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
