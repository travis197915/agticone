from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="IngestionJob",
            fields=[
                ("job_id",         models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("seed_url",       models.URLField(max_length=2048)),
                ("status",         models.CharField(choices=[("QUEUED","Queued"),("RUNNING","Running"),("COMPLETED","Completed"),("FAILED","Failed"),("PARTIAL","Partial")], db_index=True, default="QUEUED", max_length=16)),
                ("docs_queued",    models.PositiveIntegerField(default=0)),
                ("docs_processed", models.PositiveIntegerField(default=0)),
                ("docs_failed",    models.PositiveIntegerField(default=0)),
                ("max_depth",      models.PositiveSmallIntegerField(default=4)),
                ("max_docs",       models.PositiveIntegerField(default=200)),
                ("llm_provider",   models.CharField(default="anthropic", max_length=32)),
                ("llm_model",      models.CharField(default="claude-3-5-sonnet-20241022", max_length=64)),
                ("celery_task_id", models.CharField(blank=True, max_length=255)),
                ("created_at",     models.DateTimeField(auto_now_add=True)),
                ("started_at",     models.DateTimeField(blank=True, null=True)),
                ("completed_at",   models.DateTimeField(blank=True, null=True)),
                ("summary",        models.JSONField(blank=True, null=True)),
                ("errors",         models.JSONField(default=list)),
            ],
            options={"ordering": ["-created_at"], "verbose_name": "Ingestion Job"},
        ),
        migrations.CreateModel(
            name="IngestedDocument",
            fields=[
                ("id",           models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("job",          models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="documents", to="sop_ingestion.ingestionjob")),
                ("url",          models.URLField(max_length=2048)),
                ("content_hash", models.CharField(db_index=True, max_length=64)),
                ("doc_format",   models.CharField(max_length=8)),
                ("depth",        models.PositiveSmallIntegerField(default=0)),
                ("status",       models.CharField(default="OK", max_length=16)),
                ("neo4j_sop_id", models.CharField(blank=True, max_length=128)),
                ("pg_sop_id",    models.CharField(blank=True, max_length=128)),
                ("steps_count",  models.PositiveIntegerField(default=0)),
                ("rules_count",  models.PositiveIntegerField(default=0)),
                ("codes_count",  models.PositiveIntegerField(default=0)),
                ("links_found",  models.PositiveIntegerField(default=0)),
                ("created_at",   models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["depth", "created_at"]},
        ),
        migrations.AddConstraint(
            model_name="ingesteddocument",
            constraint=models.UniqueConstraint(
                fields=["job", "content_hash"],
                name="unique_job_hash_v2",
            ),
        ),
    ]
