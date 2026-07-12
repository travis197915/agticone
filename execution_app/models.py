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
        ("SKIPPED", "Skipped (duplicate or prior clean run)"),
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
    review_started_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # Identified Line of Business for this claim (SOW deliverable):
    # {"product", "network", "label", "source"}. Empty for legacy/failed runs.
    claim_lob = models.JSONField(default=dict, blank=True)
    # LLM cost snapshot for this claim, computed at end of run from
    # sop_ingestion.LLMCallLog rows tagged with this run_id. Denormalized so
    # dashboards can sort/filter by spend without scanning the LLMCallLog table.
    # ``cost_breakdown`` carries {"<provider>/<model>": {"calls", "prompt_tokens",
    # "completion_tokens", "cost_usd"}} for explainability.
    total_prompt_tokens = models.BigIntegerField(default=0)
    total_completion_tokens = models.BigIntegerField(default=0)
    total_cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    cost_breakdown = models.JSONField(default=dict, blank=True)

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
    # --- human-override / live-execution columns (present in the deployed schema) ---
    # These exist in the live DB but were missing from this model, so every insert
    # tripped the NOT-NULL "overridden" constraint and aborted the run. Declared here
    # (state matches via 0002_ruleeval_override state-only migration) so the ORM
    # populates them. ``overridden`` is the only NOT-NULL one.
    live_result = models.JSONField(null=True, blank=True)
    overridden = models.BooleanField(default=False)
    injected_context = models.JSONField(null=True, blank=True)

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


class ClaimExecutiveSummary(_UUIDPK):
    """Human-auditor executive summary for one claim run.

    Add-on output of the ``executive_summary`` engine agent (n08), also
    (re)generated by the ``backfill_executive_summary`` management command.
    Condenses every agent/shape + the final verdict into a short overall
    narrative, a few key findings, and one plain-language line per step — so a
    human auditor gets the gist in seconds instead of scrolling the full
    per-rule reasoning. Purely additive: a failure here never affects the run.
    """
    run = models.OneToOneField(
        RuleExecutionRun, on_delete=models.CASCADE, related_name="executive_summary",
    )
    claim_id = models.CharField(max_length=128, db_index=True, blank=True, default="")
    # Aggregate engine verdict (ALLOW/DENY/...) and rolled-up audit status
    # (CLEAN/DEFECT/INCONCLUSIVE) captured at generation time.
    verdict = models.CharField(max_length=32, blank=True, default="")
    audit_status = models.CharField(max_length=32, blank=True, default="")
    # One-line headline + 2-4 sentence executive narrative.
    headline = models.CharField(max_length=512, blank=True, default="")
    overall_summary = models.TextField(blank=True, default="")
    # [str] — up to a handful of key findings a human auditor should notice.
    key_findings = models.JSONField(default=list, blank=True)
    # [{shape_id, agent_name, status, summary}] — one plain-language line/step.
    step_summaries = models.JSONField(default=list, blank=True)
    llm_provider = models.CharField(max_length=32, blank=True, default="")
    llm_model = models.CharField(max_length=64, blank=True, default="")
    # "agent" (written inline by n08) | "backfill" | "fallback" (no LLM).
    generated_by = models.CharField(max_length=32, blank=True, default="agent")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "execution_claim_executive_summary"
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
    # Set when this record was served from ClaimMemory instead of a live call;
    # points at the run whose live invocation produced the reused result.
    reused_from_run = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "execution_tool_invocation"
        ordering = ["run", "called_at"]


class ClaimMemory(_UUIDPK):
    """Persistent per-claim context, scoped per (claim_id, SOP).

    One row per SOP the claim has been audited against — independent of which
    workflow chained that SOP, so memory survives workflow edits and is shared
    when the same SOP is attached to several workflows. Rules with no SOP
    share a single row with ``sop_id=""``.

    Updated after every successful run (n07 in the execution engine). Read at
    the start of every run to give the agent awareness of prior processing:
    prior per-rule verdicts + reasoning, prior tool results (reusable within a
    TTL), and the prior final output. ``drift`` accumulates every structured
    disagreement between a live evaluation and the remembered one, along with
    how it was resolved (CLAIM_MEMORY_CONFLICT_POLICY).
    """
    claim_id = models.CharField(max_length=128, db_index=True)
    # str(AuditSop.id) — a plain char column (not an FK) so deleting/re-running
    # an ingestion job never cascades away a claim's audit memory.
    sop_id = models.CharField(max_length=64, blank=True, default="")
    sop_title = models.CharField(max_length=512, blank=True, default="")
    runs_count = models.PositiveIntegerField(default=0)
    last_run = models.ForeignKey(
        RuleExecutionRun, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    last_decision_type = models.CharField(max_length=32, blank=True, default="")
    last_narrative = models.TextField(blank=True, default="")
    # sha256 of the canonical claim payload from the last run; a mismatch on
    # the next run means the claim's data changed -> tool reuse + prior-wins
    # pinning are suspended for that run and memory rebuilds from live results.
    claim_payload_hash = models.CharField(max_length=64, blank=True, default="")
    # {rule_key: {matched, skipped, confidence, reasoning, decision_type,
    #             llm_status, navigation, run_id, at}}
    rule_memory = models.JSONField(default=dict, blank=True)
    # {"{tool_name}:{sha256(args)}": {ok, result, phase, run_id, called_at}}
    tool_memory = models.JSONField(default=dict, blank=True)
    # compact, append-only: [{run_id, batch_id, workflow_id, status,
    #                         decision_type, codes, finished_at}]
    run_history = models.JSONField(default=list, blank=True)
    # [{run_id, scope: "rule"|"claim", rule_key?, prior, live,
    #   claim_data_changed, resolution, at}]
    drift = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "execution_claim_memory"
        ordering = ["-updated_at"]
        unique_together = ("claim_id", "sop_id")
