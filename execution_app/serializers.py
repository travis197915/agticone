"""DRF serializers for the GET endpoints (batch summary, run detail)."""
from __future__ import annotations

from rest_framework import serializers

from uhc_execution_engine.duplicate_claim import skip_metadata
from uhc_execution_engine.xlsx_parser import EXCEL_BILLING_FIELDS

from . import trace_builder
from .models import (BatchExecutionRun, RuleEvaluation, RuleExecutionRun,
                      ToolInvocationRecord)
from .reviewer_lookup import resolve_reviewer_names


def _processing_time_min(run: RuleExecutionRun) -> float:
    if run.started_at and run.finished_at:
        seconds = (run.finished_at - run.started_at).total_seconds()
        return round(seconds / 60, 1)
    return 0.0


def _format_time(ts) -> str:
    if ts is None:
        return ""
    return ts.strftime("%I:%M:%S %p")


def _format_iso(ts) -> str | None:
    if ts is None:
        return None
    from datetime import timezone as dt_timezone
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=dt_timezone.utc)
    return (
        ts.astimezone(dt_timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _excel_claim_fields(payload: dict | None) -> dict:
    if not payload:
        return {}
    return {
        k: payload[k]
        for k in EXCEL_BILLING_FIELDS
        if payload.get(k) not in (None, "")
    }


def serialize_run_summary(
    run: RuleExecutionRun,
    *,
    reviewer_names: dict[str, str] | None = None,
    version_info: dict[str, dict] | None = None,
) -> dict:
    """Lightweight row for batch detail and the all-runs list.

    ``reviewer_names`` lets callers batch-resolve userID -> name once for a
    whole page/batch instead of a query per row; falls back to a per-row
    lookup when omitted.

    ``version_info`` (from ``services.run_versions.run_version_info``) is the
    same idea for the rule version a run executed against. Omitted rather than
    derived per row, because a reprocessed claim produces a second row for the
    same claim id and the version is the only thing that tells them apart —
    computing that 25 times over would be 25 round trips.
    """
    if reviewer_names is None:
        reviewer_names = resolve_reviewer_names(
            [run.htl_reviewer, run.original_auditor]
        )
    version = (version_info or {}).get(str(run.id)) or {}
    return {
        # Which rules produced this row, and whether the workflow has moved on.
        # ``can_reprocess`` is the button: a re-run would use different rules.
        "version_label": version.get("version_label", ""),
        "sop_versions": version.get("sop_versions", []),
        "is_outdated": version.get("is_outdated", False),
        # True when the run wrote no evaluations: the label is the version it
        # was dispatched against, not one it executed.
        "never_ran": version.get("never_ran", False),
        "current_version_label": version.get("current_label", ""),
        "can_reprocess": version.get("is_outdated", False),
        "id": str(run.id),
        "run_id": str(run.id),
        "batch_id": str(run.batch_id) if run.batch_id else "",
        "claim_id": run.claim_id,
        "status": run.status,
        "run_status": run.status,
        "claim_status": claim_audit_status(run),
        "final_decision_type": run.final_decision_type,
        "applied_codes": run.applied_codes,
        "claim_lob": run.claim_lob or {},
        "lob_label": (run.claim_lob or {}).get("label", ""),
        "error_message": run.error_message,
        "started_at": _format_time(run.started_at),
        "started_at_date": (
            run.started_at.strftime("%Y-%m-%d") if run.started_at else ""
        ),
        "finished_at": _format_time(run.finished_at),
        "finished_at_date": (
            run.finished_at.strftime("%Y-%m-%d") if run.finished_at else ""
        ),
        "processing_time_min": _processing_time_min(run),
        "review_status": run.review_status or None,
        "auditor_status": run.auditor_status or None,
        "feedback": run.review_feedback or None,
        "review_started_at": _format_time(run.review_started_at),
        "reviewed_at": _format_time(run.reviewed_at),
        "htl_reviewer": (
            reviewer_names.get(run.htl_reviewer) or (run.htl_reviewer or None)
        ),
        "original_auditor": (
            reviewer_names.get(run.original_auditor) or (run.original_auditor or None)
        ),
        **_excel_claim_fields(run.claim_payload),
        **skip_metadata(run.claim_payload),
    }


def claim_audit_status(run: RuleExecutionRun) -> str:
    """Canonical 3-state claim status (CLEAN / DEFECT / INCONCLUSIVE).

    Mirrors ``views._claim_status`` but works off the lightweight run row +
    its stored trace (no node rollup), so the list view stays consistent with
    the detail page. A system/fetch failure is *inconclusive*, not a defect.
    """
    if run.status == "SKIPPED":
        decided = trace_builder.normalize_decision(run.final_decision_type)
        if decided:
            return decided
        return trace_builder.INCONCLUSIVE
    if run.status == "RUNNING":
        return trace_builder.IN_PROGRESS
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
    htl_reviewer = serializers.SerializerMethodField()
    original_auditor = serializers.SerializerMethodField()
    field_history = serializers.SerializerMethodField()

    class Meta:
        model = RuleExecutionRun
        fields = ["id", "batch", "workflow", "claim_id", "claim_payload",
                  "raw_fetch", "started_at", "finished_at", "status",
                  "final_decision_type", "applied_codes", "narrative",
                  "claim_lob", "error_message", "review_status", "review_feedback",
                  "auditor_status", "review_started_at", "reviewed_at",
                  "htl_reviewer", "original_auditor", "field_history",
                  "evaluations", "tool_invocations"]

    def _reviewer_names(self, run: RuleExecutionRun) -> dict[str, str]:
        cached = getattr(self, "_reviewer_names_cache", None)
        if cached is None:
            cached = resolve_reviewer_names(
                [run.htl_reviewer, run.original_auditor]
            )
            self._reviewer_names_cache = cached
        return cached

    def get_htl_reviewer(self, run: RuleExecutionRun) -> str | None:
        names = self._reviewer_names(run)
        return names.get(run.htl_reviewer) or (run.htl_reviewer or None)

    def get_original_auditor(self, run: RuleExecutionRun) -> str | None:
        names = self._reviewer_names(run)
        return names.get(run.original_auditor) or (run.original_auditor or None)

    def get_field_history(self, run: RuleExecutionRun) -> list[dict[str, object]]:
        return [
            {
                "fieldName": change.field_name,
                "oldValue": change.old_value,
                "newValue": change.new_value,
                "changedAt": _format_iso(change.changed_at),
                "changedBy": change.changed_by,
            }
            for change in run.field_changes.all()[:20]
        ]


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
        runs = list(obj.runs.select_related("trace").all())
        reviewer_names = resolve_reviewer_names(
            [r.htl_reviewer for r in runs] + [r.original_auditor for r in runs]
        )
        return [
            serialize_run_summary(r, reviewer_names=reviewer_names) for r in runs
        ]
