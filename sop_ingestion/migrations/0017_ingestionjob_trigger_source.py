"""Add IngestionJob.trigger_source.

Some shared databases already have this NOT NULL column (added outside Django).
Use IF NOT EXISTS so this migration is safe on both fresh and drifted DBs.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0016_widen_ingesteddocument_sop_ids"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE sop_ingestion_ingestionjob
                ADD COLUMN IF NOT EXISTS trigger_source varchar(32)
                    NOT NULL DEFAULT 'api';
            """,
            reverse_sql="""
                ALTER TABLE sop_ingestion_ingestionjob
                DROP COLUMN IF EXISTS trigger_source;
            """,
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="ingestionjob",
                    name="trigger_source",
                    field=models.CharField(
                        choices=[
                            ("api", "REST API"),
                            ("workflow", "Workflow builder"),
                            ("cli", "CLI / script"),
                        ],
                        db_index=True,
                        default="api",
                        max_length=32,
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]
