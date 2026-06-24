from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0008_ruleexecutionrun_auditor_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="ruleexecutionrun",
            name="review_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ruleexecutionrun",
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
