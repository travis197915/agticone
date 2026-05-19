"""Link IngestionJob → builder.Workflow (nullable)."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("sop_ingestion", "0005_audit_graph"),
        ("builder",       "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="ingestionjob",
            name="workflow",
            field=models.ForeignKey(
                to="builder.workflow",
                on_delete=django.db.models.deletion.SET_NULL,
                null=True,
                blank=True,
                related_name="ingestion_jobs",
                db_index=True,
            ),
        ),
    ]
