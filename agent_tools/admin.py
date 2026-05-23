"""Admin registrations for the agent_tools registry + bindings."""
from __future__ import annotations

from django.contrib import admin

from .models import NodeRuleBinding, NodeToolBinding, Tool


@admin.register(Tool)
class ToolAdmin(admin.ModelAdmin):
    list_display = ("display_name", "name", "kind", "is_active", "updated_at")
    list_filter = ("kind", "is_active")
    search_fields = ("name", "display_name", "description", "endpoint_id")
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("display_name", "name")


@admin.register(NodeRuleBinding)
class NodeRuleBindingAdmin(admin.ModelAdmin):
    list_display = ("rule_key", "sop", "shape", "ordering", "updated_at")
    list_select_related = ("sop", "shape")
    search_fields = ("rule_key", "condition", "action")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(NodeToolBinding)
class NodeToolBindingAdmin(admin.ModelAdmin):
    list_display = ("tool", "shape", "rule_binding", "ordering", "updated_at")
    list_select_related = ("tool", "shape", "rule_binding")
    search_fields = ("tool__name", "tool__display_name")
    readonly_fields = ("id", "created_at", "updated_at")
