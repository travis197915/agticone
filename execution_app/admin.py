from django.contrib import admin

from .models import (BatchExecutionRun, RuleEvaluation, RuleExecutionRun,
                      ToolInvocationRecord)


@admin.register(BatchExecutionRun)
class BatchExecutionRunAdmin(admin.ModelAdmin):
    list_display = ("id", "workflow", "status", "total_claims",
                    "completed", "failed", "started_at")
    list_filter = ("status",)
    readonly_fields = ("started_at", "finished_at")


@admin.register(RuleExecutionRun)
class RuleExecutionRunAdmin(admin.ModelAdmin):
    list_display = ("id", "claim_id", "workflow", "status",
                    "final_decision_type", "htl_reviewer", "started_at")
    list_filter = ("status", "final_decision_type")
    search_fields = ("claim_id", "htl_reviewer", "original_auditor")
    readonly_fields = ("started_at", "finished_at")


@admin.register(RuleEvaluation)
class RuleEvaluationAdmin(admin.ModelAdmin):
    list_display = ("run", "order_index", "rule_key", "rule_source",
                    "matched", "decision_type", "confidence")
    list_filter = ("matched", "rule_source", "decision_type")
    search_fields = ("rule_key",)


@admin.register(ToolInvocationRecord)
class ToolInvocationRecordAdmin(admin.ModelAdmin):
    list_display = ("run", "tool_name", "phase", "ok", "duration_ms",
                    "called_at")
    list_filter = ("phase", "ok", "tool_name")
