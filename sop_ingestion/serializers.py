from rest_framework import serializers
from .models import IngestionJob, IngestedDocument


class IngestedDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model  = IngestedDocument
        fields = ["url", "doc_format", "depth", "status", "neo4j_sop_id",
                  "steps_count", "rules_count", "codes_count", "links_found",
                  "created_at"]


class IngestionJobSerializer(serializers.ModelSerializer):
    documents        = IngestedDocumentSerializer(many=True, read_only=True)
    duration_seconds = serializers.SerializerMethodField()

    class Meta:
        model  = IngestionJob
        fields = ["job_id", "seed_url", "status", "docs_queued",
                  "docs_processed", "docs_failed", "max_depth", "max_docs",
                  "llm_provider", "llm_model", "celery_task_id",
                  "created_at", "started_at", "completed_at",
                  "duration_seconds", "summary", "errors", "documents"]
        read_only_fields = ["job_id", "status", "docs_queued", "docs_processed",
                            "docs_failed", "celery_task_id", "created_at",
                            "started_at", "completed_at", "summary", "errors"]

    def get_duration_seconds(self, obj):
        if obj.started_at and obj.completed_at:
            return round((obj.completed_at - obj.started_at).total_seconds(), 1)
        return None


class StartJobSerializer(serializers.Serializer):
    seed_url     = serializers.URLField()
    max_depth    = serializers.IntegerField(min_value=1, max_value=8,   default=4)
    max_docs     = serializers.IntegerField(min_value=1, max_value=500, default=200)
    llm_provider = serializers.ChoiceField(choices=["openai", "anthropic"],
                                           default="anthropic")
    llm_model    = serializers.CharField(max_length=64,
                                         default="claude-sonnet-4-5-20250929")
