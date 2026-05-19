from django.contrib import admin
from django.utils.html import format_html
from .models import IngestionJob, IngestedDocument


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
