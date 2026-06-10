"""Set database-level DEFAULTs on the 0012/0013 audit columns.

Same rationale as 0008: Django field defaults are applied in Python, not in
the database, so the pipeline's raw-SQL INSERTs fail with NOT NULL violations
whenever they omit a newer column (seen twice in production logs — first with
``is_out_of_scope``, then with ``applicable_when``). Baking the defaults into
the schema makes the writers robust to future column additions of this kind.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("sop_ingestion", "0013_auditdecision_applicable_when"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                # ── AuditStep (added in 0012) ────────────────────────────────
                "ALTER TABLE sop_ingestion_auditstep ALTER COLUMN is_out_of_scope SET DEFAULT false;",
                "ALTER TABLE sop_ingestion_auditstep ALTER COLUMN yaml_rule_id    SET DEFAULT '';",
                # ── AuditDecision (added in 0012 + 0013) ─────────────────────
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN depth             SET DEFAULT 0;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN subrule_id        SET DEFAULT '';",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN table_name        SET DEFAULT '';",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN aggregation       SET DEFAULT 'LEAF';",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN output_text       SET DEFAULT '';",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN tooling_allowed   SET DEFAULT true;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN is_out_of_scope   SET DEFAULT false;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN mongo_subtree_ref SET DEFAULT '';",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN applicable_when   SET DEFAULT '';",
            ],
            reverse_sql=[
                "ALTER TABLE sop_ingestion_auditstep ALTER COLUMN is_out_of_scope DROP DEFAULT;",
                "ALTER TABLE sop_ingestion_auditstep ALTER COLUMN yaml_rule_id    DROP DEFAULT;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN depth             DROP DEFAULT;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN subrule_id        DROP DEFAULT;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN table_name        DROP DEFAULT;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN aggregation       DROP DEFAULT;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN output_text       DROP DEFAULT;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN tooling_allowed   DROP DEFAULT;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN is_out_of_scope   DROP DEFAULT;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN mongo_subtree_ref DROP DEFAULT;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN applicable_when   DROP DEFAULT;",
            ],
        ),
    ]
