from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0020_ruleexecutionrun_field_change"),
    ]

    operations = [
        migrations.AddField(
            model_name="ruleexecutionrun",
            name="workflow_version",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ruleexecutionrun",
            name="workbench_versions",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
