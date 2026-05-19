"""
Migration 0004 — Claims Audit Schema

Replaces the generic sop_* content tables with purpose-built audit_* tables
designed around how a human claims auditor reads and follows an SOP:

  AuditSop          — the SOP document (root of the audit trail)
  AuditPrecondition — what the auditor checks before entering the decision tree
  AuditStep         — each decision point in the workflow
  AuditDecision     — one If/Then row in a step's decision table
  AuditGroupLimit   — group-specific timely filing limits
  AuditCode         — every claims code mentioned (EOB, EX, denial, system)
  AuditDateCondition — date-range conditions that affect rule applicability
  AuditAnnotation   — notes, alerts, exceptions the auditor must read
  AuditReference    — cross-references to other SOPs / documents

The old sop_* tables are dropped first (data will be re-ingested).
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0003_sop_content_tables"),
    ]

    operations = [
        # ── Drop old content tables (order respects FK constraints) ──────────
        migrations.RunSQL(
            sql="""
                DROP TABLE IF EXISTS sop_ingestion_ctxunresolved    CASCADE;
                DROP TABLE IF EXISTS sop_ingestion_ctxdatecondition CASCADE;
                DROP TABLE IF EXISTS sop_ingestion_ctxgrouprule     CASCADE;
                DROP TABLE IF EXISTS sop_ingestion_ctxcode          CASCADE;
                DROP TABLE IF EXISTS sop_ingestion_soplink          CASCADE;
                DROP TABLE IF EXISTS sop_ingestion_soprule          CASCADE;
                DROP TABLE IF EXISTS sop_ingestion_soppresection    CASCADE;
                DROP TABLE IF EXISTS sop_ingestion_sopdocument      CASCADE;
            """,
            reverse_sql="SELECT 1;",  # irreversible
        ),

        # ── AuditSop ─────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="AuditSop",
            fields=[
                ("id",                models.AutoField(primary_key=True, serialize=False)),
                ("job",               models.ForeignKey(
                                          on_delete=django.db.models.deletion.CASCADE,
                                          related_name="audit_sops",
                                          to="sop_ingestion.ingestionjob")),
                ("url",               models.TextField()),
                ("content_hash",      models.CharField(db_index=True, max_length=64)),
                ("doc_format",        models.CharField(default="HTML", max_length=8)),
                ("neo4j_sop_id",      models.CharField(blank=True, max_length=256)),
                ("title",             models.TextField(blank=True)),
                ("purpose",           models.TextField(blank=True)),
                ("llm_summary",       models.TextField(blank=True)),
                ("platform",          models.CharField(blank=True, max_length=256)),
                ("lob",               models.JSONField(default=list)),
                ("audience",          models.JSONField(default=list)),
                ("state_div",         models.CharField(blank=True, max_length=256)),
                ("product",           models.CharField(blank=True, max_length=256)),
                ("effective_date",    models.CharField(blank=True, max_length=32)),
                ("revision_date",     models.CharField(blank=True, max_length=32)),
                ("crawl_depth",       models.PositiveSmallIntegerField(default=0)),
                ("parent_url",        models.TextField(blank=True)),
                ("step_count",        models.PositiveIntegerField(default=0)),
                ("decision_count",    models.PositiveIntegerField(default=0)),
                ("code_count",        models.PositiveIntegerField(default=0)),
                ("precondition_count", models.PositiveIntegerField(default=0)),
                ("raw_text",          models.TextField(blank=True)),
                ("parse_warnings",    models.JSONField(default=list)),
                ("crawled_at",        models.DateTimeField(auto_now_add=True)),
                ("updated_at",        models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Audit SOP",
                "ordering": ["crawl_depth", "crawled_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="auditsop",
            constraint=models.UniqueConstraint(
                fields=["job", "content_hash"], name="unique_auditsop_job_hash"
            ),
        ),

        # ── AuditPrecondition ─────────────────────────────────────────────────
        migrations.CreateModel(
            name="AuditPrecondition",
            fields=[
                ("id",            models.AutoField(primary_key=True, serialize=False)),
                ("sop",           models.ForeignKey(
                                      on_delete=django.db.models.deletion.CASCADE,
                                      related_name="preconditions",
                                      to="sop_ingestion.auditsop")),
                ("display_order", models.PositiveSmallIntegerField(default=0)),
                ("category",      models.CharField(
                                      choices=[("PLATFORM","Platform"),
                                               ("AUDIENCE","Audience"),
                                               ("LOB","Line of Business"),
                                               ("ELIGIBILITY","Eligibility"),
                                               ("COVERAGE","Coverage"),
                                               ("GENERAL","General")],
                                      default="GENERAL", max_length=32)),
                ("label",         models.CharField(max_length=512)),
                ("content_text",  models.TextField(blank=True)),
                ("llm_rules",     models.JSONField(default=list)),
                ("is_blocking",   models.BooleanField(default=False)),
            ],
            options={"verbose_name": "Audit Precondition", "ordering": ["display_order"]},
        ),

        # ── AuditStep ─────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="AuditStep",
            fields=[
                ("id",                 models.AutoField(primary_key=True, serialize=False)),
                ("sop",                models.ForeignKey(
                                           on_delete=django.db.models.deletion.CASCADE,
                                           related_name="steps",
                                           to="sop_ingestion.auditsop")),
                ("step_number",        models.PositiveSmallIntegerField()),
                ("question",           models.TextField(blank=True)),
                ("intro_text",         models.TextField(blank=True)),
                ("is_terminal",        models.BooleanField(default=False)),
                ("terminal_action",    models.CharField(blank=True, max_length=64)),
                ("is_sub_procedure",   models.BooleanField(default=False)),
                ("sub_procedure_name", models.CharField(blank=True, max_length=256)),
                ("neo4j_node_id",      models.CharField(blank=True, max_length=256)),
            ],
            options={"verbose_name": "Audit Step", "ordering": ["step_number"]},
        ),
        migrations.AddConstraint(
            model_name="auditstep",
            constraint=models.UniqueConstraint(
                fields=["sop", "step_number"], name="unique_auditstep_sop_number"
            ),
        ),

        # ── AuditDecision ─────────────────────────────────────────────────────
        migrations.CreateModel(
            name="AuditDecision",
            fields=[
                ("id",             models.AutoField(primary_key=True, serialize=False)),
                ("step",           models.ForeignKey(
                                       on_delete=django.db.models.deletion.CASCADE,
                                       related_name="decisions",
                                       to="sop_ingestion.auditstep")),
                ("row_index",      models.PositiveSmallIntegerField(default=0)),
                ("condition_if",   models.TextField(blank=True)),
                ("condition_and",  models.TextField(blank=True)),
                ("action_text",    models.TextField(blank=True)),
                ("action_summary", models.TextField(blank=True)),
                ("action_line",    models.TextField(blank=True)),
                ("action_claim",   models.TextField(blank=True)),
                ("decision_type",  models.CharField(
                                       choices=[("DENY","Deny"),("ALLOW","Allow"),
                                                ("BYPASS","Bypass Edit"),
                                                ("PEND","Pend for Review"),
                                                ("REFER","Refer / Proceed"),
                                                ("SYSTEM","System Action"),
                                                ("STOP","Stop Processing"),
                                                ("WAIVE","Waive"),
                                                ("CONDITIONAL","Conditional")],
                                       db_index=True, default="CONDITIONAL",
                                       max_length=16)),
                ("goto_step",      models.SmallIntegerField(blank=True, null=True)),
                ("is_final",       models.BooleanField(default=False)),
                ("eob_codes",      models.JSONField(default=list)),
                ("ex_codes",       models.JSONField(default=list)),
                ("denial_codes",   models.JSONField(default=list)),
                ("system_actions", models.JSONField(default=list)),
                ("all_codes",      models.JSONField(default=list)),
                ("neo4j_edge_id",  models.CharField(blank=True, max_length=256)),
            ],
            options={"verbose_name": "Audit Decision",
                     "ordering": ["step__step_number", "row_index"]},
        ),

        # ── AuditGroupLimit ───────────────────────────────────────────────────
        migrations.CreateModel(
            name="AuditGroupLimit",
            fields=[
                ("id",                    models.AutoField(primary_key=True, serialize=False)),
                ("sop",                   models.ForeignKey(
                                              on_delete=django.db.models.deletion.CASCADE,
                                              related_name="group_limits",
                                              to="sop_ingestion.auditsop")),
                ("group_name",            models.CharField(db_index=True, max_length=256)),
                ("inn_days",              models.SmallIntegerField(blank=True, null=True)),
                ("oon_days",              models.SmallIntegerField(blank=True, null=True)),
                ("limit_days",            models.SmallIntegerField(blank=True, null=True)),
                ("limit_months",          models.SmallIntegerField(blank=True, null=True)),
                ("limit_years",           models.SmallIntegerField(blank=True, null=True)),
                ("calculation_basis",     models.CharField(default="DOS", max_length=32)),
                ("network_type",          models.CharField(default="BOTH", max_length=8)),
                ("member_submitted_only", models.BooleanField(default=False)),
                ("exceptions",            models.JSONField(default=list)),
                ("special_notes",         models.JSONField(default=list)),
                ("raw_text",              models.TextField(blank=True)),
            ],
            options={"verbose_name": "Audit Group Limit", "ordering": ["group_name"]},
        ),

        # ── AuditCode ─────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="AuditCode",
            fields=[
                ("id",              models.AutoField(primary_key=True, serialize=False)),
                ("sop",             models.ForeignKey(
                                        on_delete=django.db.models.deletion.CASCADE,
                                        related_name="codes",
                                        to="sop_ingestion.auditsop")),
                ("code_value",      models.CharField(max_length=64)),
                ("code_type",       models.CharField(
                                        choices=[("EOB","EOB Code"),("EX","Exception Code"),
                                                 ("DENIAL","Denial Code"),
                                                 ("SYSTEM_ACT","System Action"),
                                                 ("POS","Place of Service"),
                                                 ("REVENUE","Revenue Code"),
                                                 ("BILL_TYPE","Type of Bill"),
                                                 ("MODIFIER","Procedure Modifier"),
                                                 ("FREQUENCY","Frequency Code"),
                                                 ("CPT","CPT / HCPCS"),
                                                 ("UNKNOWN","Other")],
                                        db_index=True, max_length=16)),
                ("description",     models.TextField(blank=True)),
                ("context_snippet", models.TextField(blank=True)),
                ("source_step",     models.SmallIntegerField(blank=True, null=True)),
                ("source_field",    models.CharField(blank=True, max_length=256)),
                ("confidence",      models.FloatField(default=1.0)),
            ],
            options={"verbose_name": "Audit Code",
                     "ordering": ["code_type", "code_value"]},
        ),
        migrations.AddConstraint(
            model_name="auditcode",
            constraint=models.UniqueConstraint(
                fields=["sop", "code_value", "code_type"], name="unique_auditcode"
            ),
        ),

        # ── AuditDateCondition ────────────────────────────────────────────────
        migrations.CreateModel(
            name="AuditDateCondition",
            fields=[
                ("id",             models.AutoField(primary_key=True, serialize=False)),
                ("sop",            models.ForeignKey(
                                       on_delete=django.db.models.deletion.CASCADE,
                                       related_name="date_conditions",
                                       to="sop_ingestion.auditsop")),
                ("date_from",      models.CharField(blank=True, max_length=32)),
                ("date_to",        models.CharField(blank=True, max_length=32)),
                ("effective_date", models.CharField(blank=True, max_length=32)),
                ("context_text",   models.TextField(blank=True)),
                ("applies_to",     models.CharField(blank=True, max_length=256)),
            ],
            options={"verbose_name": "Audit Date Condition"},
        ),

        # ── AuditAnnotation ───────────────────────────────────────────────────
        migrations.CreateModel(
            name="AuditAnnotation",
            fields=[
                ("id",               models.AutoField(primary_key=True, serialize=False)),
                ("sop",              models.ForeignKey(
                                         on_delete=django.db.models.deletion.CASCADE,
                                         related_name="annotations",
                                         to="sop_ingestion.auditsop")),
                ("step",             models.ForeignKey(
                                         blank=True, null=True,
                                         on_delete=django.db.models.deletion.SET_NULL,
                                         related_name="annotations",
                                         to="sop_ingestion.auditstep")),
                ("annotation_type",  models.CharField(
                                         choices=[("NOTE","Note"),("ALERT","Alert"),
                                                  ("EXCEPTION","Exception"),("TIP","Tip"),
                                                  ("WARNING","Warning"),
                                                  ("HIGHLIGHT","Highlighted")],
                                         default="NOTE", max_length=16)),
                ("content_text",     models.TextField()),
                ("is_claim_impact",  models.BooleanField(default=False)),
            ],
            options={"verbose_name": "Audit Annotation", "ordering": ["annotation_type"]},
        ),

        # ── AuditReference ────────────────────────────────────────────────────
        migrations.CreateModel(
            name="AuditReference",
            fields=[
                ("id",           models.AutoField(primary_key=True, serialize=False)),
                ("sop",          models.ForeignKey(
                                     on_delete=django.db.models.deletion.CASCADE,
                                     related_name="references",
                                     to="sop_ingestion.auditsop")),
                ("step",         models.ForeignKey(
                                     blank=True, null=True,
                                     on_delete=django.db.models.deletion.SET_NULL,
                                     related_name="references",
                                     to="sop_ingestion.auditstep")),
                ("ref_text",     models.TextField(blank=True)),
                ("ref_url",      models.TextField(blank=True)),
                ("ref_type",     models.CharField(default="UNRESOLVED", max_length=32)),
                ("is_resolved",  models.BooleanField(default=False)),
            ],
            options={"verbose_name": "Audit Reference"},
        ),
    ]
