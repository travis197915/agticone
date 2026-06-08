"""DRF serializers for the GET endpoints (batch summary, run detail)."""
from __future__ import annotations

from rest_framework import serializers

from . import trace_builder
from .models import (BatchExecutionRun, RuleEvaluation, RuleExecutionRun,
                      ToolInvocationRecord)


def claim_audit_status(run: RuleExecutionRun) -> str:
    """Canonical 3-state claim status (CLEAN / DEFECT / INCONCLUSIVE).

    Mirrors ``views._claim_status`` but works off the lightweight run row +
    its stored trace (no node rollup), so the list view stays consistent with
    the detail page. A system/fetch failure is *inconclusive*, not a defect.
    """
    if run.status == "RUNNING":
        return trace_builder.INCONCLUSIVE
    if run.status in {"FAILED", "FETCH_FAILED"}:
        return trace_builder.INCONCLUSIVE
    if run.status == "TERMINATED_EARLY":
        return trace_builder.DEFECT
    # The engine's aggregated verdict is authoritative: ALLOW → CLEAN,
    # DENY/REFER/PEND/STOP → DEFECT. Intermediate Not-Met sub-checks never
    # by themselves make a claim a defect.
    decided = trace_builder.normalize_decision(run.final_decision_type)
    if decided:
        return decided
    trace = getattr(run, "trace", None)
    if trace is not None:
        if trace.final_status:
            return trace_builder.normalize_status(trace.final_status)
        if trace.trace_json:
            return trace_builder.claim_status(trace.trace_json)
    return trace_builder.INCONCLUSIVE


class ToolInvocationRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = ToolInvocationRecord
        fields = ["id", "tool_name", "phase", "args", "ok", "result",
                  "error", "duration_ms", "called_at"]


class RuleEvaluationSerializer(serializers.ModelSerializer):
    class Meta:
        model = RuleEvaluation
        fields = ["id", "order_index", "rule_key", "rule_source", "condition",
                  "action", "matched", "skipped", "skip_reason", "confidence",
                  "reasoning", "decision_type", "codes", "tool_results_used",
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
        # select_related('trace') so claim_audit_status doesn't fan out into a
        # per-run query for the reverse OneToOne.
        return [{
            "id": str(r.id),
            "claim_id": r.claim_id,
            "status": r.status,
            "claim_status": claim_audit_status(r),
            "final_decision_type": r.final_decision_type,
            "applied_codes": r.applied_codes,
            "error_message": r.error_message,
        } for r in obj.runs.select_related("trace").all()]
