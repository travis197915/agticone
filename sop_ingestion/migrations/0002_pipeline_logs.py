from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0001_initial"),
    ]

    operations = [
        # ── New fields on IngestionJob ────────────────────────────────────────
        migrations.AddField(
            model_name="ingestionjob",
            name="total_llm_calls",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="ingestionjob",
            name="total_tokens_in",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="ingestionjob",
            name="total_tokens_out",
            field=models.PositiveIntegerField(default=0),
        ),

        # ── PipelineStageLog ──────────────────────────────────────────────────
        migrations.CreateModel(
            name="PipelineStageLog",
            fields=[
                ("id",           models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("job",          models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                   related_name="stage_logs",
                                                   to="sop_ingestion.ingestionjob")),
                ("stage_name",   models.CharField(db_index=True, max_length=64)),
                ("doc_url",      models.TextField(blank=True)),
                ("doc_format",   models.CharField(blank=True, max_length=8)),
                ("doc_depth",    models.SmallIntegerField(blank=True, null=True)),
                ("started_at",   models.DateTimeField()),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("duration_ms",  models.IntegerField(blank=True, null=True)),
                ("status",       models.CharField(
                                     choices=[("OK","OK"), ("ERROR","Error"), ("SKIP","Skipped")],
                                     default="OK", max_length=8)),
                ("error_detail", models.TextField(blank=True)),
            ],
            options={"ordering": ["started_at"], "verbose_name": "Pipeline Stage Log"},
        ),

        # ── LLMCallLog ────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="LLMCallLog",
            fields=[
                ("id",                models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("job",               models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                        related_name="llm_calls",
                                                        to="sop_ingestion.ingestionjob")),
                ("stage",             models.CharField(default="enrich_stage", max_length=64)),
                ("agent_name",        models.CharField(max_length=128)),
                ("llm_provider",      models.CharField(max_length=32)),
                ("llm_model",         models.CharField(max_length=64)),
                ("prompt_tokens",     models.PositiveIntegerField(default=0)),
                ("completion_tokens", models.PositiveIntegerField(default=0)),
                ("total_tokens",      models.PositiveIntegerField(default=0)),
                ("duration_ms",       models.IntegerField(default=0)),
                ("success",           models.BooleanField(default=True)),
                ("error_message",     models.TextField(blank=True)),
                ("called_at",         models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["called_at"], "verbose_name": "LLM Call Log"},
        ),
    ]
