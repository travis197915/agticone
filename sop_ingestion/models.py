from __future__ import annotations
import uuid
from django.db import models
from django.db.models import Sum
from django.utils import timezone


class JobStatus(models.TextChoices):
    QUEUED    = "QUEUED",    "Queued"
    RUNNING   = "RUNNING",   "Running"
    COMPLETED = "COMPLETED", "Completed"
    FAILED    = "FAILED",    "Failed"
    PARTIAL   = "PARTIAL",   "Partial"


class IngestionJob(models.Model):
    job_id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Optional FK to the builder Workflow that triggered this ingestion.
    # Nullable so existing standalone ingest runs keep working.
    workflow       = models.ForeignKey(
        "builder.Workflow",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="ingestion_jobs",
        db_index=True,
    )
    seed_url       = models.URLField(max_length=2048)
    status         = models.CharField(max_length=16, choices=JobStatus.choices,
                                      default=JobStatus.QUEUED, db_index=True)
    docs_queued    = models.PositiveIntegerField(default=0)
    docs_processed = models.PositiveIntegerField(default=0)
    docs_failed    = models.PositiveIntegerField(default=0)
    max_depth      = models.PositiveSmallIntegerField(default=4)
    max_docs       = models.PositiveIntegerField(default=200)
    llm_provider   = models.CharField(max_length=32, default="anthropic")
    llm_model      = models.CharField(max_length=64,  default="claude-sonnet-4-5-20250929")
    updated_at     = models.DateTimeField(auto_now=True)
    celery_task_id = models.CharField(max_length=255, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)
    started_at     = models.DateTimeField(null=True, blank=True)
    completed_at   = models.DateTimeField(null=True, blank=True)
    summary        = models.JSONField(null=True, blank=True)
    errors         = models.JSONField(default=list)
    # LLM usage aggregates — updated after each pipeline run
    total_llm_calls       = models.PositiveIntegerField(default=0)
    total_tokens_in       = models.PositiveIntegerField(default=0)
    total_tokens_out      = models.PositiveIntegerField(default=0)

    class Meta:
        ordering     = ["-created_at"]
        verbose_name = "Ingestion Job"

    def __str__(self):
        return f"[{self.status}] {self.seed_url[:60]}"

    def mark_started(self):
        self.status     = JobStatus.RUNNING
        self.started_at = timezone.now()
        self.save(update_fields=["status", "started_at"])

    def mark_done(self, summary: dict, errors: list):
        n_proc = summary.get("total_docs_processed", self.docs_processed)
        n_err  = len(errors)
        self.status         = (JobStatus.FAILED   if n_err and not n_proc else
                               JobStatus.PARTIAL   if n_err else
                               JobStatus.COMPLETED)
        self.completed_at   = timezone.now()
        self.docs_processed = n_proc
        self.docs_failed    = n_err
        self.summary        = summary
        self.errors         = errors[:200]
        self.save(update_fields=["status", "completed_at", "docs_processed",
                                 "docs_failed", "summary", "errors"])

    def mark_failed(self, reason: str):
        self.status       = JobStatus.FAILED
        self.completed_at = timezone.now()
        self.errors       = [{"agent": "system", "msg": reason}]
        self.save(update_fields=["status", "completed_at", "errors"])

    def refresh_llm_totals(self):
        """Recompute LLM aggregate counters from LLMCallLog rows."""
        agg = self.llm_calls.aggregate(
            calls=models.Count("id"),
            tin=Sum("prompt_tokens"),
            tout=Sum("completion_tokens"),
        )
        self.total_llm_calls  = agg["calls"] or 0
        self.total_tokens_in  = agg["tin"]   or 0
        self.total_tokens_out = agg["tout"]  or 0
        self.save(update_fields=["total_llm_calls", "total_tokens_in", "total_tokens_out"])


class IngestedDocument(models.Model):
    job          = models.ForeignKey(IngestionJob, on_delete=models.CASCADE,
                                     related_name="documents")
    url          = models.URLField(max_length=2048)
    content_hash = models.CharField(max_length=64, db_index=True)
    doc_format   = models.CharField(max_length=8)
    depth        = models.PositiveSmallIntegerField(default=0)
    status       = models.CharField(max_length=16, default="OK")
    neo4j_sop_id = models.CharField(max_length=128, blank=True)
    pg_sop_id    = models.CharField(max_length=128, blank=True)
    steps_count  = models.PositiveIntegerField(default=0)
    rules_count  = models.PositiveIntegerField(default=0)
    codes_count  = models.PositiveIntegerField(default=0)
    links_found  = models.PositiveIntegerField(default=0)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering        = ["depth", "created_at"]
        unique_together = [("job", "content_hash")]

    def __str__(self):
        return f"{self.doc_format} {self.url[:60]}"


class StageStatus(models.TextChoices):
    OK    = "OK",    "OK"
    ERROR = "ERROR", "Error"
    SKIP  = "SKIP",  "Skipped"


class PipelineStageLog(models.Model):
    """One row per LangGraph node execution (real-time, written by psycopg2)."""

    job         = models.ForeignKey(IngestionJob, on_delete=models.CASCADE,
                                    related_name="stage_logs")
    stage_name  = models.CharField(max_length=64, db_index=True)
    doc_url     = models.TextField(blank=True)
    doc_format  = models.CharField(max_length=8, blank=True)
    doc_depth   = models.SmallIntegerField(null=True, blank=True)
    started_at  = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    status      = models.CharField(max_length=8, choices=StageStatus.choices,
                                   default=StageStatus.OK)
    error_detail = models.TextField(blank=True)

    class Meta:
        ordering     = ["started_at"]
        verbose_name = "Pipeline Stage Log"

    def __str__(self):
        return f"{self.stage_name} [{self.status}] job={self.job_id}"


class LLMCallLog(models.Model):
    """One row per LLM API call.

    Originally only the SOP-ingestion pipeline wrote here (FK = IngestionJob).
    The execution engine also writes here now — its rows have ``job=NULL``
    and reference an ``execution_app.RuleExecutionRun`` via ``execution_run``
    instead. Exactly one of ``job`` or ``execution_run`` should be set.
    """

    job               = models.ForeignKey(IngestionJob, on_delete=models.CASCADE,
                                          related_name="llm_calls",
                                          null=True, blank=True)
    execution_run     = models.ForeignKey(
        "execution_app.RuleExecutionRun", on_delete=models.CASCADE,
        related_name="llm_calls", null=True, blank=True,
    )
    stage             = models.CharField(max_length=64, default="enrich_stage")
    agent_name        = models.CharField(max_length=128)
    llm_provider      = models.CharField(max_length=32)
    llm_model         = models.CharField(max_length=64)
    prompt_tokens     = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    total_tokens      = models.PositiveIntegerField(default=0)
    duration_ms       = models.IntegerField(default=0)
    success           = models.BooleanField(default=True)
    error_message     = models.TextField(blank=True)
    called_at         = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering     = ["called_at"]
        verbose_name = "LLM Call Log"

    def __str__(self):
        return (f"{self.llm_provider}/{self.llm_model} "
                f"{self.agent_name} [{self.total_tokens} tok]")


# ── Claims Audit Tables ────────────────────────────────────────────────────────
# Schema designed around how a human claims auditor reads and follows an SOP.
# A human auditor:
#   1. Checks pre-conditions (platform, LOB, eligibility)
#   2. Follows the step-by-step decision tree
#   3. At each step reads the If/Then table and picks a branch
#   4. Arrives at a terminal action (DENY/BYPASS/PEND + specific codes)
# Neo4j holds the graph edges (Step-[:IF_YES]->Step) for traversal.
# Postgres holds the full relational record for querying and reporting.

class AuditSop(models.Model):
    """
    One ingested SOP document.  The root of every audit trail.
    A claims auditor opens this record first to understand what policy
    they are applying before they touch a single claim.
    """
    job            = models.ForeignKey(IngestionJob, on_delete=models.CASCADE,
                                       related_name="audit_sops")
    # Identity
    url            = models.TextField()
    content_hash   = models.CharField(max_length=64, db_index=True)
    doc_format     = models.CharField(max_length=8, default="HTML")
    neo4j_sop_id   = models.CharField(max_length=256, blank=True)
    # Human-readable header
    title          = models.TextField(blank=True)
    purpose        = models.TextField(blank=True)   # LLM one-liner: what this SOP governs
    llm_summary    = models.TextField(blank=True)   # LLM 3-4 sentence executive summary
    # Long-form story written by the narrative agent after graph extraction.
    # Reads like an intro to the SOP — purpose, audience, high-level walk-
    # through of pre-conditions → decision tree → terminal actions.
    narrative_context = models.TextField(blank=True, default="")
    # Applicability — who/what this SOP applies to
    platform       = models.CharField(max_length=256, blank=True)
    lob            = models.JSONField(default=list)       # ["Commercial", "Medicare Advantage"]
    audience       = models.JSONField(default=list)       # ["Claims Examiners"]
    state_div      = models.CharField(max_length=256, blank=True)
    product        = models.CharField(max_length=256, blank=True)
    # Effective dates
    effective_date = models.CharField(max_length=32, blank=True)
    revision_date  = models.CharField(max_length=32, blank=True)
    # Crawl context
    crawl_depth    = models.PositiveSmallIntegerField(default=0)
    parent_url     = models.TextField(blank=True)
    # Counts (updated after write)
    step_count     = models.PositiveIntegerField(default=0)
    decision_count = models.PositiveIntegerField(default=0)  # total If/Then rows
    code_count     = models.PositiveIntegerField(default=0)
    precondition_count = models.PositiveIntegerField(default=0)
    # Raw content
    raw_text       = models.TextField(blank=True)
    parse_warnings = models.JSONField(default=list)
    # Timestamps
    crawled_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("job", "content_hash")]
        ordering        = ["crawl_depth", "crawled_at"]
        verbose_name    = "Audit SOP"

    def __str__(self):
        return f"{self.title or self.url[:60]}"


class AuditPrecondition(models.Model):
    """
    Things the auditor verifies BEFORE entering the decision tree.
    Typically: platform, LOB, audience, eligibility rules.
    Maps to the pre-sections and business table in the HTML.
    """
    CATEGORY_CHOICES = [
        ("PLATFORM",    "Platform"),
        ("AUDIENCE",    "Audience"),
        ("LOB",         "Line of Business"),
        ("ELIGIBILITY", "Eligibility"),
        ("COVERAGE",    "Coverage"),
        ("GENERAL",     "General"),
    ]
    sop            = models.ForeignKey(AuditSop, on_delete=models.CASCADE,
                                       related_name="preconditions")
    display_order  = models.PositiveSmallIntegerField(default=0)
    category       = models.CharField(max_length=32, choices=CATEGORY_CHOICES,
                                      default="GENERAL")
    label          = models.CharField(max_length=512)   # e.g. "Lines of Business"
    content_text   = models.TextField(blank=True)       # the actual rule text
    llm_rules      = models.JSONField(default=list)     # LLM-extracted rules from this section
    is_blocking    = models.BooleanField(default=False) # if fails → do not proceed

    class Meta:
        ordering     = ["display_order"]
        verbose_name = "Audit Precondition"

    def __str__(self):
        return f"{self.label}: {self.content_text[:60]}"


class AuditStep(models.Model):
    """
    One decision point in the audit workflow.
    A human auditor reads the question, consults the If/Then table,
    picks a branch, and moves to the next step or terminates.
    Neo4j holds the Step→Step edges for graph traversal.
    """
    sop              = models.ForeignKey(AuditSop, on_delete=models.CASCADE,
                                         related_name="steps")
    step_number      = models.PositiveSmallIntegerField()
    # What the auditor reads at this step
    question         = models.TextField(blank=True)  # the main question/instruction
    intro_text       = models.TextField(blank=True)  # preamble before the If/Then table
    # Terminal steps end the audit path
    is_terminal      = models.BooleanField(default=False)  # F3/F4 step = end
    terminal_action  = models.CharField(max_length=64, blank=True)  # "Process claim (F3)"
    # Sub-procedure steps belong to a named sub-flow
    is_sub_procedure = models.BooleanField(default=False)
    sub_procedure_name = models.CharField(max_length=256, blank=True)
    # Neo4j reference
    neo4j_node_id    = models.CharField(max_length=256, blank=True)
    # LLM-generated per-step narrative: a 2-3 sentence paragraph that
    # explains the purpose of this step, how the auditor evaluates the
    # If/Then rows, and where the flow goes next. Populated by the
    # narrative agent after graph extraction. Optional — falls back to
    # `intro_text` when empty.
    narrative_context = models.TextField(blank=True, default="")

    class Meta:
        unique_together = [("sop", "step_number")]
        ordering        = ["step_number"]
        verbose_name    = "Audit Step"

    def __str__(self):
        return f"Step {self.step_number}: {self.question[:60]}"


class AuditDecision(models.Model):
    """
    One row in the If/Then decision table for a step.
    This is the atomic unit of claims audit logic:
      IF <condition> [AND <additional_condition>] THEN <action>
    The auditor reads these rows, finds the matching condition,
    and executes the action (deny, bypass, pend, etc.).
    """
    DECISION_CHOICES = [
        ("DENY",        "Deny"),
        ("ALLOW",       "Allow"),
        ("BYPASS",      "Bypass Edit"),
        ("PEND",        "Pend for Review"),
        ("REFER",       "Refer / Proceed"),
        ("SYSTEM",      "System Action"),
        ("STOP",        "Stop Processing"),
        ("WAIVE",       "Waive"),
        ("CONDITIONAL", "Conditional"),
    ]
    step             = models.ForeignKey(AuditStep, on_delete=models.CASCADE,
                                         related_name="decisions")
    row_index        = models.PositiveSmallIntegerField(default=0)
    # The condition the auditor evaluates
    condition_if     = models.TextField(blank=True)
    condition_and    = models.TextField(blank=True)   # second AND column in 3-col tables
    # The full action text as written in the SOP
    action_text      = models.TextField(blank=True)
    # LLM-enriched summaries
    action_summary   = models.TextField(blank=True)  # one-line summary
    action_line      = models.TextField(blank=True)  # line-level override detail
    action_claim     = models.TextField(blank=True)  # claim-level override detail
    # Decision classification
    decision_type    = models.CharField(max_length=16, choices=DECISION_CHOICES,
                                        default="CONDITIONAL", db_index=True)
    # Navigation — where does this row send the auditor?
    goto_step        = models.SmallIntegerField(null=True, blank=True)
    is_final         = models.BooleanField(default=False)  # terminates audit path
    # Codes the auditor must apply
    eob_codes        = models.JSONField(default=list)   # ["E51", "F51"]
    ex_codes         = models.JSONField(default=list)   # ["003", "020"]
    denial_codes     = models.JSONField(default=list)   # ["346", "CDD"]
    system_actions   = models.JSONField(default=list)   # ["F3", "F4", "F5"]
    all_codes        = models.JSONField(default=list)   # merged, for quick lookup
    # Neo4j edge reference
    neo4j_edge_id    = models.CharField(max_length=256, blank=True)

    class Meta:
        ordering     = ["step__step_number", "row_index"]
        verbose_name = "Audit Decision"

    def __str__(self):
        return f"Step {self.step.step_number} row {self.row_index}: [{self.decision_type}]"


class AuditGroupLimit(models.Model):
    """
    Group-specific timely filing limits.
    A claims auditor looks up the claim's group here to determine
    how many days from DOS (or paid date) the provider had to submit.
    """
    sop                   = models.ForeignKey(AuditSop, on_delete=models.CASCADE,
                                              related_name="group_limits")
    group_name            = models.CharField(max_length=256, db_index=True)
    # In-network vs out-of-network limits (days)
    inn_days              = models.SmallIntegerField(null=True, blank=True)
    oon_days              = models.SmallIntegerField(null=True, blank=True)
    # Fallback unified limit if INN/OON not split
    limit_days            = models.SmallIntegerField(null=True, blank=True)
    limit_months          = models.SmallIntegerField(null=True, blank=True)
    limit_years           = models.SmallIntegerField(null=True, blank=True)
    # Calculation basis
    calculation_basis     = models.CharField(max_length=32, default="DOS",
                                             help_text="DOS, PAID_DATE, or EOB_DATE")
    network_type          = models.CharField(max_length=8, default="BOTH")
    member_submitted_only = models.BooleanField(default=False)
    # Exceptions and special notes
    exceptions            = models.JSONField(default=list)
    special_notes         = models.JSONField(default=list)
    raw_text              = models.TextField(blank=True)

    class Meta:
        ordering     = ["group_name"]
        verbose_name = "Audit Group Limit"

    def __str__(self):
        return f"{self.group_name}: INN={self.inn_days}d OON={self.oon_days}d"


class AuditCode(models.Model):
    """
    Every claims code mentioned in the SOP.
    The auditor uses this as a reference to know exactly which
    EOB code, EX code, or denial code to apply in each scenario.
    """
    CODE_TYPE_CHOICES = [
        ("EOB",        "EOB Code (E/F/W)"),
        ("EX",         "Exception Code"),
        ("DENIAL",     "Denial Code"),
        ("SYSTEM_ACT", "System Action (F3/F4/F5)"),
        ("POS",        "Place of Service"),
        ("REVENUE",    "Revenue Code"),
        ("BILL_TYPE",  "Type of Bill"),
        ("MODIFIER",   "Procedure Modifier"),
        ("FREQUENCY",  "Frequency Code"),
        ("CPT",        "CPT / HCPCS"),
        ("UNKNOWN",    "Other"),
    ]
    sop             = models.ForeignKey(AuditSop, on_delete=models.CASCADE,
                                        related_name="codes")
    code_value      = models.CharField(max_length=64)
    code_type       = models.CharField(max_length=16, choices=CODE_TYPE_CHOICES,
                                       db_index=True)
    description     = models.TextField(blank=True)
    context_snippet = models.TextField(blank=True)  # surrounding sentence
    source_step     = models.SmallIntegerField(null=True, blank=True)
    source_field    = models.CharField(max_length=256, blank=True)
    confidence      = models.FloatField(default=1.0)

    class Meta:
        unique_together = [("sop", "code_value", "code_type")]
        ordering        = ["code_type", "code_value"]
        verbose_name    = "Audit Code"

    def __str__(self):
        return f"[{self.code_type}] {self.code_value}"


class AuditDateCondition(models.Model):
    """
    Date-range conditions that affect which rules apply.
    e.g. "For DOS on or after 01/01/2024 apply the new timely filing rule."
    """
    sop            = models.ForeignKey(AuditSop, on_delete=models.CASCADE,
                                       related_name="date_conditions")
    date_from      = models.CharField(max_length=32, blank=True)
    date_to        = models.CharField(max_length=32, blank=True)
    effective_date = models.CharField(max_length=32, blank=True)
    context_text   = models.TextField(blank=True)
    applies_to     = models.CharField(max_length=256, blank=True)  # section / step

    class Meta:
        verbose_name = "Audit Date Condition"


class AuditAnnotation(models.Model):
    """
    Notes, alerts, and exceptions embedded in the SOP.
    Auditors must read these before making a final decision.
    """
    TYPE_CHOICES = [
        ("NOTE",      "Note"),
        ("ALERT",     "Alert"),
        ("EXCEPTION", "Exception"),
        ("TIP",       "Tip"),
        ("WARNING",   "Warning"),
        ("HIGHLIGHT", "Highlighted"),
    ]
    sop              = models.ForeignKey(AuditSop, on_delete=models.CASCADE,
                                         related_name="annotations")
    step             = models.ForeignKey(AuditStep, on_delete=models.SET_NULL,
                                         null=True, blank=True,
                                         related_name="annotations")
    annotation_type  = models.CharField(max_length=16, choices=TYPE_CHOICES,
                                        default="NOTE")
    content_text     = models.TextField()
    is_claim_impact  = models.BooleanField(default=False)  # directly affects claim decision

    class Meta:
        ordering     = ["annotation_type"]
        verbose_name = "Audit Annotation"


class AuditReference(models.Model):
    """
    Cross-references to other SOPs, calculators, or policy documents.
    An auditor may need to consult these before completing the audit.
    """
    sop          = models.ForeignKey(AuditSop, on_delete=models.CASCADE,
                                     related_name="references")
    step         = models.ForeignKey(AuditStep, on_delete=models.SET_NULL,
                                     null=True, blank=True,
                                     related_name="references")
    ref_text     = models.TextField(blank=True)
    ref_url      = models.TextField(blank=True)
    ref_type     = models.CharField(max_length=32, default="UNRESOLVED")
    is_resolved  = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Audit Reference"


# ─────────────────────────────────────────────────────────────────────────────
# Materialised Knowledge Graph — explicit nodes & edges per SOP
#
# Every other table holds business data. These two tables hold the SAME data
# re-expressed as graph entities so that:
#   • the Cytoscape viewer queries one place
#   • downstream audit reasoners can traverse the graph without joins
#   • Neo4j (or any external graph DB) can be hydrated from these rows
#
# Node types : DOCUMENT, META, PRE_SECTION, PRE_RULE, STEP, DECISION,
#              ANNOTATION, GROUP_LIMIT, CODE, DATE_COND, REFERENCE
# Edge types : HAS_META, HAS_PRE_SECTION, HAS_RULE, HAS_STEP, HAS_DECISION,
#              HAS_ANNOTATION, HAS_GROUP_LIMIT, HAS_CODE_REF, HAS_DATE_COND,
#              REFERENCES, GOTO
# ─────────────────────────────────────────────────────────────────────────────


class GraphNodeType(models.TextChoices):
    DOCUMENT    = "DOCUMENT",    "Document"
    META        = "META",        "Metadata"
    PRE_SECTION = "PRE_SECTION", "Pre-Section"
    PRE_RULE    = "PRE_RULE",    "Pre-Section Rule"
    STEP        = "STEP",        "Step"
    DECISION    = "DECISION",    "Decision Rule"
    ANNOTATION  = "ANNOTATION",  "Annotation"
    GROUP_LIMIT = "GROUP_LIMIT", "Group Limit"
    CODE        = "CODE",        "Claims Code"
    DATE_COND   = "DATE_COND",   "Date Condition"
    REFERENCE   = "REFERENCE",   "Cross-Reference"


class GraphEdgeRel(models.TextChoices):
    HAS_META         = "HAS_META",         "has metadata"
    HAS_PRE_SECTION  = "HAS_PRE_SECTION",  "has pre-section"
    HAS_RULE         = "HAS_RULE",         "has rule"
    HAS_STEP         = "HAS_STEP",         "has step"
    HAS_DECISION     = "HAS_DECISION",     "has decision"
    HAS_ANNOTATION   = "HAS_ANNOTATION",   "has annotation"
    HAS_GROUP_LIMIT  = "HAS_GROUP_LIMIT",  "has group limit"
    HAS_CODE_REF     = "HAS_CODE_REF",     "has code reference"
    HAS_DATE_COND    = "HAS_DATE_COND",    "has date condition"
    REFERENCES       = "REFERENCES",       "references other SOP"
    GOTO             = "GOTO",             "branches to step"


class AuditGraphNode(models.Model):
    """One node in the persisted knowledge graph for an SOP."""
    sop         = models.ForeignKey(AuditSop, on_delete=models.CASCADE,
                                    related_name="graph_nodes")
    # Stable identifier within an SOP — e.g. "doc", "step_3", "dec_42"
    node_key    = models.CharField(max_length=128, db_index=True)
    node_type   = models.CharField(max_length=24, choices=GraphNodeType.choices,
                                   db_index=True)
    label       = models.CharField(max_length=255)
    # Per-type structured payload — varies (step_number, decision_type, codes, etc.)
    details     = models.JSONField(default=dict, blank=True)
    # Optional back-references to the source rows so we can navigate back
    ref_table   = models.CharField(max_length=64, blank=True)
    ref_id      = models.PositiveIntegerField(null=True, blank=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Audit Graph Node"
        unique_together = [("sop", "node_key")]
        indexes = [
            models.Index(fields=["sop", "node_type"]),
        ]

    def __str__(self) -> str:
        return f"[{self.node_type}] {self.label[:60]}"


class AuditGraphEdge(models.Model):
    """One directed edge in the persisted knowledge graph for an SOP."""
    sop      = models.ForeignKey(AuditSop, on_delete=models.CASCADE,
                                 related_name="graph_edges")
    source   = models.ForeignKey(AuditGraphNode, on_delete=models.CASCADE,
                                 related_name="edges_out")
    target   = models.ForeignKey(AuditGraphNode, on_delete=models.CASCADE,
                                 related_name="edges_in")
    rel_type = models.CharField(max_length=24, choices=GraphEdgeRel.choices,
                                db_index=True)
    label    = models.CharField(max_length=255, blank=True)
    details  = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Audit Graph Edge"
        indexes = [
            models.Index(fields=["sop", "rel_type"]),
            models.Index(fields=["source", "rel_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.source.node_key} -[{self.rel_type}]-> {self.target.node_key}"


class SopExclusion(models.Model):
    """User-marked exclusion attached to a SOP.

    Distinct from LLM-derived exclusions (``AuditPrecondition.llm_rules[*]
    .is_exception``): this row lets an auditor explicitly say "ignore this
    rule / step / section when evaluating this SOP".

    ``target_kind`` + ``target_key`` together identify the excluded thing
    using the same stable keys the builder ``/attachable/`` endpoint emits:

    ============  ===========================================================
    target_kind   target_key
    ============  ===========================================================
    rule          rule_key (e.g. ``step:22:4:0`` or ``pre:22:88:1``)
    step          ``step:<sop_id>:<step_number>``  — fans out to every
                  decision row in that step
    section       ``pre:<sop_id>:<precondition_id>`` — fans out to every
                  ``llm_rule`` in that pre-condition section
    sop           ``sop:<sop_id>`` — the whole SOP is excluded
    graph_node    raw ``AuditGraphNode.node_key`` (e.g. ``step_4_d0``,
                  ``pre_3_r5``) — used for graph-native picks
    ============  ===========================================================

    (sop, target_kind, target_key) is unique so toggling on/off is a stable
    upsert/delete.
    """

    TARGET_KINDS = [
        ("rule",       "Rule"),
        ("step",       "Step"),
        ("section",    "Pre-condition section"),
        ("sop",        "Whole SOP"),
        ("graph_node", "Graph node"),
        ("html_block", "Raw HTML block"),
    ]

    sop          = models.ForeignKey(AuditSop, on_delete=models.CASCADE,
                                     related_name="user_exclusions")
    target_kind  = models.CharField(max_length=16, choices=TARGET_KINDS, default="rule")
    target_key   = models.CharField(max_length=255, db_index=True)
    label        = models.CharField(max_length=255, blank=True, default="",
                       help_text="Human-readable label rendered in the SPA")
    reason       = models.TextField(blank=True, default="",
                       help_text="Free-form note from the auditor")
    snippet_text = models.TextField(blank=True, default="",
                       help_text="Captured source text for the excluded thing")
    metadata     = models.JSONField(default=dict, blank=True,
                       help_text="Free-form bag (section_label, graph_node_key …)")
    created_by_id    = models.CharField(max_length=64, blank=True, default="")
    created_by_email = models.EmailField(blank=True, default="")
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "SOP Exclusion"
        ordering     = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["sop", "target_kind", "target_key"],
                name="uniq_sop_exclusion_target",
            ),
        ]
        indexes = [
            models.Index(fields=["sop", "target_kind"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return f"{self.target_kind}:{self.target_key} (sop={self.sop_id})"
