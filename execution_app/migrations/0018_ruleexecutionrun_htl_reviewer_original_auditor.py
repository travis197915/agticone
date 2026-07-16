# Generated manually for HTL reviewer + original auditor attribution

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0017_alter_ruleexecutionrun_status_claimexecutivesummary"),
    ]

    operations = [
        migrations.AddField(
            model_name="ruleexecutionrun",
            name="htl_reviewer",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="ruleexecutionrun",
            name="original_auditor",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
