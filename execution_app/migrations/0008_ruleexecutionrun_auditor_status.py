from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0007_ruleexecutionrun_review_feedback"),
    ]

    operations = [
        migrations.AddField(
            model_name="ruleexecutionrun",
            name="auditor_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Not started"),
                    ("PENDING", "Pending"),
                    ("IN_PROGRESS", "In progress"),
                    ("APPROVED", "Approved"),
                    ("REJECTED", "Rejected"),
                    ("COMPLETED", "Completed"),
                ],
                default="",
                max_length=32,
            ),
        ),
    ]
