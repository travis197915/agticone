"""Add the additive ClaimTrace table (trace + explainability per run)."""
import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0002_ruleevaluation_verdict"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClaimTrace",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("claim_id", models.CharField(blank=True, db_index=True,
                                              default="", max_length=128)),
                ("final_status", models.CharField(blank=True, default="",
                                                  max_length=32)),
                ("trace_json", models.JSONField(blank=True, default=list)),
                ("explainability_json", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("run", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="trace", to="execution_app.ruleexecutionrun")),
            ],
            options={
                "db_table": "execution_claim_trace",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="claimtrace",
            index=models.Index(fields=["claim_id"],
                               name="execution_c_claim_i_idx"),
        ),
    ]
