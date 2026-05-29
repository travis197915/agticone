"""Document the `verdict` column on `execution_rule_evaluation`.

The column was introduced in production out-of-band (no migration in this
repo prior to this one). Until now Django was unaware of it, so
``RuleEvaluation.objects.bulk_create(...)`` omitted it from the INSERT
and Postgres rejected the row on its NOT NULL constraint, rolling back
the surrounding atomic block in ``n07_persist_respond``.

This migration uses ``SeparateDatabaseAndState`` so the field is added to
Django's model state (matching ``execution_app.models.RuleEvaluation``)
without re-issuing ``ALTER TABLE`` against environments where the column
already exists. Fresh dev databases will need to be reset so the field
appears in ``0001_initial``'s introspected state at apply time.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name="ruleevaluation",
                    name="verdict",
                    field=models.CharField(blank=True, default="", max_length=32),
                ),
            ],
        ),
    ]
