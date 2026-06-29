"""Set database-level DEFAULT 1 on the version/revision columns.

``AuditSop.version`` and ``AuditDecision.revision`` (added in 0022) carry a
Django ``default=1``, but Django defaults are applied in Python, not in the
database. The raw-SQL INSERTs in the ingestion pipeline writers
(``a11_write_postgres``) omit these columns, so a NOT NULL violation was
raised on every fresh ingest. Adding the DEFAULT at the database layer makes
the schema robust to those writers (same pattern as 0008/0014).
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("sop_ingestion", "0022_auditdecision_revision_auditsop_version"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "ALTER TABLE sop_ingestion_auditsop      ALTER COLUMN version  SET DEFAULT 1;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN revision SET DEFAULT 1;",
                "UPDATE sop_ingestion_auditsop      SET version  = 1 WHERE version  IS NULL;",
                "UPDATE sop_ingestion_auditdecision SET revision = 1 WHERE revision IS NULL;",
            ],
            reverse_sql=[
                "ALTER TABLE sop_ingestion_auditsop      ALTER COLUMN version  DROP DEFAULT;",
                "ALTER TABLE sop_ingestion_auditdecision ALTER COLUMN revision DROP DEFAULT;",
            ],
        ),
    ]
