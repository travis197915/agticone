"""Add `skipped` / `skip_reason` to `execution_rule_evaluation`.

Routing-aware execution records rules whose step was skipped (goto /
out-of-scope) or that were not applicable. These rows carry ``skipped=True``
so the claim verdict can exclude them and the UI can grey them out instead of
showing them as failures.

Additive columns with defaults, so existing rows keep working.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0003_claimtrace"),
    ]

    operations = [
        migrations.AddField(
            model_name="ruleevaluation",
            name="skipped",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="ruleevaluation",
            name="skip_reason",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
