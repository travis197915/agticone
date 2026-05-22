"""Set database-level DEFAULT '' on narrative_context columns.

The Django field has `default=""` but Django defaults are applied in
Python, not in the database. Raw-SQL INSERTs from the pipeline writers
that omit the column would fail with a NOT NULL violation. Adding the
DEFAULT at the database layer makes the schema robust to that.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("sop_ingestion", "0007_narrative_context"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "ALTER TABLE sop_ingestion_auditsop  ALTER COLUMN narrative_context SET DEFAULT '';",
                "ALTER TABLE sop_ingestion_auditstep ALTER COLUMN narrative_context SET DEFAULT '';",
                "UPDATE sop_ingestion_auditsop  SET narrative_context = '' WHERE narrative_context IS NULL;",
                "UPDATE sop_ingestion_auditstep SET narrative_context = '' WHERE narrative_context IS NULL;",
            ],
            reverse_sql=[
                "ALTER TABLE sop_ingestion_auditsop  ALTER COLUMN narrative_context DROP DEFAULT;",
                "ALTER TABLE sop_ingestion_auditstep ALTER COLUMN narrative_context DROP DEFAULT;",
            ],
        ),
    ]
