from django.contrib import admin
from django.utils.html import format_html
from .models import (
    IngestionJob, IngestedDocument,
    AuditSop, AuditStep, AuditDecision,
)


class IngestedDocumentInline(admin.TabularInline):
    model       = IngestedDocument
    extra       = 0
    can_delete  = False
    max_num     = 100
    fields      = ["url", "doc_format", "depth", "status",
                   "steps_count", "rules_count", "codes_count", "links_found"]
    readonly_fields = fields


@admin.register(IngestionJob)
class IngestionJobAdmin(admin.ModelAdmin):
    list_display    = ["short_id", "seed_url_link", "status_badge",
                       "docs_processed", "docs_failed", "created_at", "duration"]
    list_filter     = ["status", "llm_provider", "created_at"]
    search_fields   = ["seed_url", "job_id"]
    readonly_fields = ["job_id", "status", "celery_task_id", "created_at",
                       "started_at", "completed_at", "docs_queued",
                       "docs_processed", "docs_failed", "summary", "errors"]
    inlines         = [IngestedDocumentInline]
    ordering        = ["-created_at"]

    @admin.display(description="Job ID")
    def short_id(self, obj):
        return str(obj.job_id)[:8] + "…"

    @admin.display(description="Seed URL")
    def seed_url_link(self, obj):
        return format_html('<a href="{u}" target="_blank">{u}</a>',
                           u=obj.seed_url[:80])

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "QUEUED":    "#6c757d",
            "RUNNING":   "#0d6efd",
            "COMPLETED": "#198754",
            "PARTIAL":   "#e6a817",
            "FAILED":    "#dc3545",
        }
        c = colours.get(obj.status, "#999")
        return format_html(
            '<span style="background:{c};color:#fff;padding:2px 10px;'
            'border-radius:4px;font-size:11px;font-weight:700">{s}</span>',
            c=c, s=obj.status,
        )

    @admin.display(description="Duration")
    def duration(self, obj):
        if obj.started_at and obj.completed_at:
            secs = int((obj.completed_at - obj.started_at).total_seconds())
            return f"{secs // 60}m {secs % 60}s"
        return "—"


@admin.register(IngestedDocument)
class IngestedDocumentAdmin(admin.ModelAdmin):
    list_display    = ["url", "doc_format", "depth", "status",
                       "steps_count", "rules_count", "neo4j_sop_id", "created_at"]
    list_filter     = ["doc_format", "status", "depth"]
    search_fields   = ["url", "neo4j_sop_id", "job__job_id"]
    readonly_fields = ["job", "url", "content_hash", "doc_format", "depth",
                       "status", "neo4j_sop_id", "pg_sop_id",
                       "steps_count", "rules_count", "codes_count",
                       "links_found", "created_at"]


# ── Claims-audit rule tree (incl. YAML-imported nested rules) ─────────────────

class AuditDecisionInline(admin.TabularInline):
    model        = AuditDecision
    extra        = 0
    fields       = ["depth", "subrule_id", "parent", "row_index",
                    "decision_type", "is_out_of_scope", "goto_step",
                    "condition_if", "action_text"]
    readonly_fields = fields
    ordering     = ["depth", "row_index"]
    show_change_link = True


@admin.register(AuditStep)
class AuditStepAdmin(admin.ModelAdmin):
    list_display  = ["sop", "step_number", "yaml_rule_id", "oos_badge",
                     "is_terminal", "question_short"]
    list_filter   = ["is_out_of_scope", "is_terminal", "sop"]
    search_fields = ["yaml_rule_id", "question", "sop__title"]
    inlines       = [AuditDecisionInline]

    @admin.display(description="Question")
    def question_short(self, obj):
        return (obj.question or "")[:80]

    @admin.display(description="Scope", boolean=False)
    def oos_badge(self, obj):
        if obj.is_out_of_scope:
            return format_html(
                '<span style="background:#e11d48;color:#fff;padding:1px 8px;'
                'border-radius:4px;font-size:10px;font-weight:700">OUT OF SCOPE</span>'
            )
        return "—"


@admin.register(AuditDecision)
class AuditDecisionAdmin(admin.ModelAdmin):
    list_display  = ["sop_title", "step_no", "subrule_id", "depth", "row_index",
                     "decision_type", "aggregation", "oos_badge", "goto_step"]
    list_filter   = ["is_out_of_scope", "decision_type", "depth", "aggregation"]
    search_fields = ["subrule_id", "condition_if", "action_text",
                     "step__sop__title"]
    raw_id_fields = ["step", "parent"]

    @admin.display(description="SOP")
    def sop_title(self, obj):
        return obj.step.sop.title

    @admin.display(description="Step")
    def step_no(self, obj):
        return obj.step.step_number

    @admin.display(description="Scope")
    def oos_badge(self, obj):
        if obj.is_out_of_scope:
            return format_html(
                '<span style="background:#e11d48;color:#fff;padding:1px 8px;'
                'border-radius:4px;font-size:10px;font-weight:700">OOS</span>'
            )
        return "—"


@admin.register(AuditSop)
class AuditSopAdmin(admin.ModelAdmin):
    list_display  = ["id", "title", "platform", "step_count",
                     "decision_count", "code_count"]
    search_fields = ["title", "url"]
