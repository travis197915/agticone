"""Add the canonical-IR control-plane head: SopIRDocument.

Hand-written (focused) on purpose. ``makemigrations`` wanted to bundle a large
amount of pre-existing model/migration drift (legacy Ctx*/Sop* model deletions,
BigAutoField id alters, and — critically — removal of the named unique
constraints ``unique_auditsop_job_hash`` / ``unique_auditstep_sop_number`` that
the ingestion pipeline's ``ON CONFLICT ON CONSTRAINT`` SQL still depends on).
That drift predates this feature and is intentionally NOT carried here so this
migration is safe to apply in isolation.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0014_audit_columns_db_defaults"),
    ]

    operations = [
        migrations.CreateModel(
            name="SopIRDocument",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("ir_version", models.PositiveIntegerField(default=1)),
                (
                    "content_hash",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=64
                    ),
                ),
                ("mongo_ref", models.CharField(blank=True, default="", max_length=128)),
                ("source", models.CharField(blank=True, default="", max_length=32)),
                (
                    "validation_status",
                    models.CharField(
                        choices=[
                            ("OK", "Validated"),
                            ("FLAGGED", "Validated with routing issues"),
                            ("UNCHECKED", "Persisted without validation"),
                        ],
                        default="UNCHECKED",
                        max_length=16,
                    ),
                ),
                ("validation_errors", models.JSONField(blank=True, default=list)),
                ("rule_count", models.PositiveIntegerField(default=0)),
                ("step_count", models.PositiveIntegerField(default=0)),
                ("decision_count", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ir_documents",
                        to="sop_ingestion.ingestionjob",
                    ),
                ),
                (
                    "sop",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ir_documents",
                        to="sop_ingestion.auditsop",
                    ),
                ),
            ],
            options={
                "verbose_name": "SOP IR Document",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="sopirdocument",
            index=models.Index(
                fields=["sop", "-ir_version"], name="sop_ingesti_sop_id_043acc_idx"
            ),
        ),
        migrations.AlterUniqueTogether(
            name="sopirdocument",
            unique_together={("sop", "ir_version")},
        ),
    ]
