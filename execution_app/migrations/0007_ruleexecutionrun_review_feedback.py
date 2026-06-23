from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0006_ruleevaluation_verdict_column"),
    ]

    operations = [
        migrations.AddField(
            model_name="ruleexecutionrun",
            name="review_feedback",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AlterField(
            model_name="ruleexecutionrun",
            name="review_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Not started"),
                    ("pending", "Pending review"),
                    ("in_progress", "In progress"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                    ("completed", "Completed"),
                ],
                default="",
                max_length=32,
            ),
        ),
    ]
