"""Reconcile migration state with the current models (legacy Ctx*/Sop* removal).

Two independent groups of changes:

1. **Audit\\* reconciliation (real DB operations).** The deployed schema carries
   the named unique constraints (``unique_auditcode`` etc.) and int PKs; the
   models now express the uniqueness via ``unique_together`` and use
   ``BigAutoField``. These run as normal operations — the constraints exist and
   the tables are empty, so the swaps/ALTERs apply cleanly. The three missing
   ``auditgraph*`` indexes are also added here.

2. **Legacy ``Ctx*`` / ``Sop*`` model removal (STATE-ONLY + idempotent DROP).**
   These models were deleted from the codebase but lingered in migration state.
   Critically, their tables were never physically created in this database (a
   ``makemigrations`` ``AlterUniqueTogether`` therefore failed with *"Found wrong
   number (0) of constraints for sop_ingestion_ctxcode"*). So the field/constraint
   removals and ``DeleteModel`` ops are applied to Django's **state only**, while
   the DB side is a single idempotent ``DROP TABLE IF EXISTS ... CASCADE`` that is
   a no-op where the tables are absent (here) and a clean drop where they exist
   (other environments).
"""
from django.db import migrations, models


_LEGACY_TABLES = [
    "sop_ingestion_ctxcode",
    "sop_ingestion_ctxdatecondition",
    "sop_ingestion_ctxgrouprule",
    "sop_ingestion_ctxunresolved",
    "sop_ingestion_soplink",
    "sop_ingestion_soppresection",
    "sop_ingestion_soprule",
    "sop_ingestion_sopdocument",
]

_DROP_LEGACY_SQL = "\n".join(
    f"DROP TABLE IF EXISTS {t} CASCADE;" for t in _LEGACY_TABLES
)


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0019_ingestionjob_trigger_source_db"),
    ]

    operations = [
        # ── 1. Audit* reconciliation (real DB ops) ───────────────────────────
        migrations.RemoveConstraint(
            model_name="auditcode",
            name="unique_auditcode",
        ),
        migrations.RemoveConstraint(
            model_name="auditsop",
            name="unique_auditsop_job_hash",
        ),
        migrations.RemoveConstraint(
            model_name="auditstep",
            name="unique_auditstep_sop_number",
        ),
        migrations.AlterField(
            model_name="auditannotation",
            name="id",
            field=models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        migrations.AlterField(
            model_name="auditcode",
            name="code_type",
            field=models.CharField(
                choices=[
                    ("EOB", "EOB Code (E/F/W)"),
                    ("EX", "Exception Code"),
                    ("DENIAL", "Denial Code"),
                    ("SYSTEM_ACT", "System Action (F3/F4/F5)"),
                    ("POS", "Place of Service"),
                    ("REVENUE", "Revenue Code"),
                    ("BILL_TYPE", "Type of Bill"),
                    ("MODIFIER", "Procedure Modifier"),
                    ("FREQUENCY", "Frequency Code"),
                    ("CPT", "CPT / HCPCS"),
                    ("UNKNOWN", "Other"),
                ],
                db_index=True,
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="auditcode",
            name="id",
            field=models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        migrations.AlterField(
            model_name="auditdatecondition",
            name="id",
            field=models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        migrations.AlterField(
            model_name="auditdecision",
            name="id",
            field=models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        migrations.AlterField(
            model_name="auditgraphedge",
            name="id",
            field=models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        migrations.AlterField(
            model_name="auditgraphedge",
            name="rel_type",
            field=models.CharField(
                choices=[
                    ("HAS_META", "has metadata"),
                    ("HAS_PRE_SECTION", "has pre-section"),
                    ("HAS_RULE", "has rule"),
                    ("HAS_STEP", "has step"),
                    ("HAS_DECISION", "has decision"),
                    ("HAS_ANNOTATION", "has annotation"),
                    ("HAS_GROUP_LIMIT", "has group limit"),
                    ("HAS_CODE_REF", "has code reference"),
                    ("HAS_DATE_COND", "has date condition"),
                    ("REFERENCES", "references other SOP"),
                    ("GOTO", "branches to step"),
                ],
                db_index=True,
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="auditgraphnode",
            name="id",
            field=models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        migrations.AlterField(
            model_name="auditgraphnode",
            name="node_type",
            field=models.CharField(
                choices=[
                    ("DOCUMENT", "Document"),
                    ("META", "Metadata"),
                    ("PRE_SECTION", "Pre-Section"),
                    ("PRE_RULE", "Pre-Section Rule"),
                    ("STEP", "Step"),
                    ("DECISION", "Decision Rule"),
                    ("ANNOTATION", "Annotation"),
                    ("GROUP_LIMIT", "Group Limit"),
                    ("CODE", "Claims Code"),
                    ("DATE_COND", "Date Condition"),
                    ("REFERENCE", "Cross-Reference"),
                ],
                db_index=True,
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="auditgrouplimit",
            name="calculation_basis",
            field=models.CharField(
                default="DOS", help_text="DOS, PAID_DATE, or EOB_DATE", max_length=32
            ),
        ),
        migrations.AlterField(
            model_name="auditgrouplimit",
            name="id",
            field=models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        migrations.AlterField(
            model_name="auditprecondition",
            name="id",
            field=models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        migrations.AlterField(
            model_name="auditreference",
            name="id",
            field=models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        migrations.AlterField(
            model_name="auditsop",
            name="id",
            field=models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        migrations.AlterField(
            model_name="auditstep",
            name="id",
            field=models.BigAutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        migrations.AlterUniqueTogether(
            name="auditcode",
            unique_together={("sop", "code_value", "code_type")},
        ),
        migrations.AlterUniqueTogether(
            name="auditsop",
            unique_together={("job", "content_hash")},
        ),
        migrations.AlterUniqueTogether(
            name="auditstep",
            unique_together={("sop", "step_number")},
        ),
        migrations.AddIndex(
            model_name="auditgraphedge",
            index=models.Index(
                fields=["sop", "rel_type"], name="sop_ingesti_sop_id_1ce800_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="auditgraphedge",
            index=models.Index(
                fields=["source", "rel_type"], name="sop_ingesti_source__e3ebdb_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="auditgraphnode",
            index=models.Index(
                fields=["sop", "node_type"], name="sop_ingesti_sop_id_515809_idx"
            ),
        ),
        # ── 2. Legacy Ctx*/Sop* removal: STATE-ONLY + idempotent DROP TABLE ──
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=_DROP_LEGACY_SQL,
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
            state_operations=[
                migrations.AlterUniqueTogether(name="ctxcode", unique_together=None),
                migrations.RemoveField(model_name="ctxcode", name="document"),
                migrations.RemoveField(model_name="ctxdatecondition", name="document"),
                migrations.RemoveField(model_name="ctxgrouprule", name="document"),
                migrations.RemoveField(model_name="ctxunresolved", name="job"),
                migrations.AlterUniqueTogether(name="sopdocument", unique_together=None),
                migrations.RemoveField(model_name="sopdocument", name="job"),
                migrations.RemoveField(model_name="soplink", name="source_document"),
                migrations.RemoveField(model_name="soppresection", name="document"),
                migrations.RemoveField(model_name="soprule", name="document"),
                migrations.DeleteModel(name="CtxCode"),
                migrations.DeleteModel(name="CtxDateCondition"),
                migrations.DeleteModel(name="CtxGroupRule"),
                migrations.DeleteModel(name="CtxUnresolved"),
                migrations.DeleteModel(name="SopLink"),
                migrations.DeleteModel(name="SopPreSection"),
                migrations.DeleteModel(name="SopDocument"),
                migrations.DeleteModel(name="SopRule"),
            ],
        ),
    ]
