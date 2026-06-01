from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0012_revision_check_scheduler"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditsop",
            name="activation_status",
            field=models.CharField(
                choices=[
                    ("active", "Active"),
                    ("pending_review", "Pending review"),
                    ("rejected", "Rejected"),
                    ("superseded", "Superseded"),
                ],
                db_index=True,
                default="active",
                max_length=16,
            ),
        ),
    ]
