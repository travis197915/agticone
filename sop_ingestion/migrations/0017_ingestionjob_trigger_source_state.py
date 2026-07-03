"""Track the out-of-band ``trigger_source`` column in Django's model state.

Another service added ``trigger_source`` (varchar(32), NOT NULL, no default)
to the shared ``sop_ingestion_ingestionjob`` table, but this branch's model
never declared it — so every ORM insert omitted the column and Postgres
rejected the row on its NOT NULL constraint (breaking SOP auto-build, which
creates IngestionJob rows).

This migration adds the field to Django's *state only* via
``SeparateDatabaseAndState`` — the column already exists in the DB, so no
``ALTER TABLE`` is issued (safe against the shared production database). With
the field now known to the model (default "manual", matching all existing
rows), inserts populate it and succeed.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0016_widen_ingesteddocument_sop_ids"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name="ingestionjob",
                    name="trigger_source",
                    field=models.CharField(default="manual", max_length=32),
                ),
            ],
        ),
    ]
