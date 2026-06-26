from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0004_ruleevaluation_skipped"),
    ]

    operations = [
        migrations.AddField(
            model_name="ruleexecutionrun",
            name="review_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Not started"),
                    ("pending", "Pending review"),
                    ("in_progress", "In progress"),
                    ("completed", "Completed"),
                ],
                default="",
                max_length=32,
            ),
        ),
    ]
