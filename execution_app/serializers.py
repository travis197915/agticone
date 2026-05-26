"""DRF serializers for the GET endpoints (batch summary, run detail)."""
from __future__ import annotations

from rest_framework import serializers

from .models import (BatchExecutionRun, RuleEvaluation, RuleExecutionRun,
                      ToolInvocationRecord)


class ToolInvocationRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = ToolInvocationRecord
        fields = ["id", "tool_name", "phase", "args", "ok", "result",
                  "error", "duration_ms", "called_at"]


class RuleEvaluationSerializer(serializers.ModelSerializer):
    class Meta:
        model = RuleEvaluation
        fields = ["id", "order_index", "rule_key", "rule_source", "condition",
                  "action", "matched", "confidence", "reasoning",
                  "decision_type", "codes", "tool_results_used",
                  "llm_provider", "llm_ms"]


class RuleExecutionRunSerializer(serializers.ModelSerializer):
    evaluations = RuleEvaluationSerializer(many=True, read_only=True)
    tool_invocations = ToolInvocationRecordSerializer(many=True, read_only=True)

    class Meta:
        model = RuleExecutionRun
        fields = ["id", "batch", "workflow", "claim_id", "claim_payload",
                  "raw_fetch", "started_at", "finished_at", "status",
                  "final_decision_type", "applied_codes", "narrative",
                  "error_message", "evaluations", "tool_invocations"]


class BatchExecutionRunSerializer(serializers.ModelSerializer):
    runs = serializers.SerializerMethodField()

    class Meta:
        model = BatchExecutionRun
        fields = ["id", "workflow", "source_filename", "claim_id_column",
                  "total_claims", "completed", "failed",
                  "started_at", "finished_at", "status", "error_message",
                  "runs"]

    def get_runs(self, obj):
        return [{
            "id": str(r.id),
            "claim_id": r.claim_id,
            "status": r.status,
            "final_decision_type": r.final_decision_type,
            "applied_codes": r.applied_codes,
            "error_message": r.error_message,
        } for r in obj.runs.all()]
