"""Persistence for batch + per-claim rule executions.

Layout:
    BatchExecutionRun       one row per uploaded .xlsx
    └── RuleExecutionRun    one row per claim (may exist standalone for single-claim runs)
        ├── RuleEvaluation       one row per evaluated rule (precondition or decision)
        └── ToolInvocationRecord one row per tool call (outer FETCH/PARSE + inner EVALUATE)

All FKs to `agent_tools` use SET_NULL so dropping/renaming a binding
doesn't delete history.
"""
from __future__ import annotations

import uuid

from django.db import models


class _UUIDPK(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class BatchExecutionRun(_UUIDPK):
    STATUS_CHOICES = [
        ("RUNNING", "Running"),
        ("COMPLETED", "Completed"),
        ("PARTIAL", "Partial"),
        ("FAILED", "Failed"),
    ]

    workflow = models.ForeignKey(
        "builder.Workflow", on_delete=models.PROTECT, related_name="execution_batches",
    )
    source_filename = models.CharField(max_length=512, blank=True, default="")
    claim_id_column = models.CharField(max_length=128, default="claim_id")
    total_claims = models.PositiveIntegerField(default=0)
    completed = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="RUNNING")
    error_message = models.TextField(blank=True, default="")

    class Meta:
        db_table = "execution_batch_run"
        ordering = ["-started_at"]


class RuleExecutionRun(_UUIDPK):
    STATUS_CHOICES = [
        ("RUNNING", "Running"),
        ("COMPLETED", "Completed"),
        ("FAILED", "Failed"),
        ("TERMINATED_EARLY", "Terminated early (DENY/STOP rule on a shape)"),
        ("FETCH_FAILED", "Claim fetch failed"),
    ]
    REVIEW_STATUS_CHOICES = [
        ("", "Not started"),
        ("pending", "Pending review"),
        ("in_progress", "In progress"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("completed", "Completed"),
    ]
    AUDITOR_STATUS_CHOICES = [
        ("", "Not started"),
        ("PENDING", "Pending"),
        ("IN_PROGRESS", "In progress"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("COMPLETED", "Completed"),
    ]

    batch = models.ForeignKey(
        BatchExecutionRun, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="runs",
    )
    workflow = models.ForeignKey(
        "builder.Workflow", on_delete=models.PROTECT, related_name="execution_runs",
    )
    claim_id = models.CharField(max_length=128, db_index=True, blank=True, default="")
    claim_payload = models.JSONField(default=dict, blank=True)
    raw_fetch = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default="RUNNING")
    final_decision_type = models.CharField(max_length=32, blank=True, default="")
    applied_codes = models.JSONField(default=list, blank=True)
    narrative = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    review_status = models.CharField(
        max_length=32, choices=REVIEW_STATUS_CHOICES, blank=True, default="",
    )
    review_feedback = models.TextField(blank=True, default="")
    auditor_status = models.CharField(
        max_length=32, choices=AUDITOR_STATUS_CHOICES, blank=True, default="",
    )

    class Meta:
        db_table = "execution_rule_run"
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["batch", "claim_id"])]


class RuleEvaluation(models.Model):
    SOURCE_CHOICES = [("PRECONDITION", "Precondition"), ("DECISION", "Decision")]

    run = models.ForeignKey(RuleExecutionRun, on_delete=models.CASCADE,
                            related_name="evaluations")
    order_index = models.PositiveIntegerField(default=0)
    rule_binding = models.ForeignKey(
        "agent_tools.NodeRuleBinding", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    rule_key = models.CharField(max_length=255, db_index=True)
    rule_source = models.CharField(max_length=16, choices=SOURCE_CHOICES,
                                   default="DECISION")
    condition = models.TextField(blank=True, default="")
    action = models.TextField(blank=True, default="")
    matched = models.BooleanField(default=False)
    # True when the rule was NOT evaluated against Met/Not-Met because routing
    # skipped its step (goto/out-of-scope) or it was not applicable. Skipped
    # rows are excluded from the claim verdict and shown greyed in the UI.
    skipped = models.BooleanField(default=False)
    skip_reason = models.CharField(max_length=255, blank=True, default="")
    confidence = models.FloatField(default=0.0)
    reasoning = models.TextField(blank=True, default="")
    decision_type = models.CharField(max_length=32, blank=True, default="")
    verdict = models.CharField(max_length=32, blank=True, default="")
    codes = models.JSONField(default=list, blank=True)
    tool_results_used = models.JSONField(default=list, blank=True)
    llm_provider = models.CharField(max_length=32, blank=True, default="")
    llm_ms = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "execution_rule_evaluation"
        ordering = ["run", "order_index"]


class ClaimTrace(_UUIDPK):
    """Denormalized trace + explainability log for one claim run.

    Additive: written after the existing run/evaluation/tool rows persist
    (guarded so a trace failure never affects the run). Stores the two arrays
    in the ``trace.json`` / ``explainability.json`` shapes the UI consumes.
    """
    run = models.OneToOneField(
        RuleExecutionRun, on_delete=models.CASCADE, related_name="trace",
    )
    claim_id = models.CharField(max_length=128, db_index=True, blank=True, default="")
    final_status = models.CharField(max_length=32, blank=True, default="")
    trace_json = models.JSONField(default=list, blank=True)
    explainability_json = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "execution_claim_trace"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["claim_id"])]


class ToolInvocationRecord(models.Model):
    PHASE_CHOICES = [("FETCH", "Fetch"), ("PARSE", "Parse"), ("EVALUATE", "Evaluate")]

    run = models.ForeignKey(RuleExecutionRun, on_delete=models.CASCADE,
                            related_name="tool_invocations")
    tool_binding = models.ForeignKey(
        "agent_tools.NodeToolBinding", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    tool_name = models.CharField(max_length=255, db_index=True)
    phase = models.CharField(max_length=16, choices=PHASE_CHOICES, default="EVALUATE")
    args = models.JSONField(default=dict, blank=True)
    ok = models.BooleanField(default=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    duration_ms = models.PositiveIntegerField(default=0)
    called_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "execution_tool_invocation"
        ordering = ["run", "called_at"]
