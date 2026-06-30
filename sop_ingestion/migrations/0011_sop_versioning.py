from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0010_sop_exclusion_html_block"),
    ]

    operations = [
        migrations.CreateModel(
            name="SopDocument",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("canonical_url", models.TextField(unique=True)),
                ("title", models.TextField(blank=True)),
                ("latest_revision_date", models.CharField(blank=True, max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "SOP Document",
                "ordering": ["-updated_at"],
            },
        ),
        migrations.AddField(
            model_name="auditsop",
            name="canonical_url",
            field=models.TextField(blank=True, db_index=True),
        ),
        migrations.AddField(
            model_name="auditsop",
            name="version_number",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="auditsop",
            name="is_current",
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.AddField(
            model_name="auditsop",
            name="version_action",
            field=models.CharField(blank=True, default="", max_length=24),
        ),
        migrations.AddField(
            model_name="auditsop",
            name="document",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="versions",
                to="sop_ingestion.sopdocument",
            ),
        ),
        migrations.AddField(
            model_name="auditsop",
            name="supersedes",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="superseded_by_set",
                to="sop_ingestion.auditsop",
            ),
        ),
        migrations.AddField(
            model_name="sopdocument",
            name="current_version",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="sop_ingestion.auditsop",
            ),
        ),
        migrations.CreateModel(
            name="SopVersionDiff",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("from_revision_date", models.CharField(blank=True, max_length=32)),
                ("to_revision_date", models.CharField(blank=True, max_length=32)),
                ("from_content_hash", models.CharField(blank=True, max_length=64)),
                ("to_content_hash", models.CharField(blank=True, max_length=64)),
                ("summary", models.JSONField(default=dict)),
                ("changes", models.JSONField(default=list)),
                ("computed_at", models.DateTimeField(auto_now_add=True)),
                ("document", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="diffs",
                    to="sop_ingestion.sopdocument",
                )),
                ("from_sop", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="diffs_from",
                    to="sop_ingestion.auditsop",
                )),
                ("to_sop", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="diffs_to",
                    to="sop_ingestion.auditsop",
                )),
            ],
            options={
                "verbose_name": "SOP Version Diff",
                "ordering": ["-computed_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="sopversiondiff",
            constraint=models.UniqueConstraint(
                fields=("from_sop", "to_sop"),
                name="uniq_sop_version_diff_pair",
            ),
        ),
    ]
