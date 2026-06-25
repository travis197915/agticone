"""Reconcile model state with the deployed ``execution_rule_evaluation`` schema.

The live database already carries ``live_result`` (jsonb null), ``overridden``
(boolean NOT NULL) and ``injected_context`` (jsonb null) — added by a deployed
human-override / live-execution feature — but this codebase's ``RuleEvaluation``
model never declared them. Because the model omitted ``overridden`` (NOT NULL,
no DB default), every insert raised ``IntegrityError`` and aborted the run.

This migration adds the fields to Django's *state only* (the columns already
exist in the DB), so the ORM populates them on insert without attempting a
duplicate ``ADD COLUMN``.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0004_ruleevaluation_skipped"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name="ruleevaluation",
                    name="live_result",
                    field=models.JSONField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name="ruleevaluation",
                    name="overridden",
                    field=models.BooleanField(default=False),
                ),
                migrations.AddField(
                    model_name="ruleevaluation",
                    name="injected_context",
                    field=models.JSONField(blank=True, null=True),
                ),
            ],
        ),
    ]
