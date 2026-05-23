# Generated for SopExclusion (manually trimmed — unrelated model drift was
# pruned out by hand; this migration only creates the new SopExclusion table).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0008_narrative_context_db_default"),
    ]

    operations = [
        migrations.CreateModel(
            name="SopExclusion",
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
                (
                    "target_kind",
                    models.CharField(
                        choices=[
                            ("rule", "Rule"),
                            ("step", "Step"),
                            ("section", "Pre-condition section"),
                            ("sop", "Whole SOP"),
                            ("graph_node", "Graph node"),
                        ],
                        default="rule",
                        max_length=16,
                    ),
                ),
                ("target_key", models.CharField(db_index=True, max_length=255)),
                (
                    "label",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Human-readable label rendered in the SPA",
                        max_length=255,
                    ),
                ),
                (
                    "reason",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Free-form note from the auditor",
                    ),
                ),
                (
                    "snippet_text",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Captured source text for the excluded thing",
                    ),
                ),
                (
                    "metadata",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Free-form bag (section_label, graph_node_key …)",
                    ),
                ),
                (
                    "created_by_id",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "created_by_email",
                    models.EmailField(blank=True, default="", max_length=254),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "sop",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="user_exclusions",
                        to="sop_ingestion.auditsop",
                    ),
                ),
            ],
            options={
                "verbose_name": "SOP Exclusion",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="sopexclusion",
            index=models.Index(
                fields=["sop", "target_kind"], name="sop_ingesti_sop_id_9f52d3_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="sopexclusion",
            constraint=models.UniqueConstraint(
                fields=("sop", "target_kind", "target_key"),
                name="uniq_sop_exclusion_target",
            ),
        ),
    ]
