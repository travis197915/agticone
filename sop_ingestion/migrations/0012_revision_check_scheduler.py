from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0011_sop_versioning"),
    ]

    operations = [
        migrations.AddField(
            model_name="ingestionjob",
            name="trigger_source",
            field=models.CharField(
                blank=True,
                default="manual",
                help_text="manual | workflow | revision_check",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="sopdocument",
            name="last_remote_content_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="sopdocument",
            name="last_remote_revision_date",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="sopdocument",
            name="last_revision_check_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sopdocument",
            name="last_revision_check_detail",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="sopdocument",
            name="last_revision_check_status",
            field=models.CharField(blank=True, max_length=32),
        ),
    ]
