"""Physically reconcile `execution_rule_evaluation` columns.

Migrations ``0002_ruleevaluation_verdict`` and ``0005_ruleeval_override_state``
added ``verdict`` / ``live_result`` / ``overridden`` / ``injected_context`` to
Django's *model state only* (``SeparateDatabaseAndState`` with empty
``database_operations``), on the assumption the columns already existed in the
deployed Postgres schema.

That assumption does not hold for fresh / local databases: the columns were
never physically created, so ``RuleEvaluation.objects.bulk_create(...)`` issued
an INSERT naming columns that do not exist and Postgres aborted the row —
rolling back the atomic block in ``n07_persist_respond`` and failing the run.

This migration issues idempotent ``ADD COLUMN IF NOT EXISTS`` statements
(database-only; the model state is already correct from the prior migrations)
so every environment converges on the same physical schema. It is a no-op on
databases where the columns already exist.
"""

from django.db import migrations


_ADD_COLUMNS = [
    "ALTER TABLE execution_rule_evaluation "
    "ADD COLUMN IF NOT EXISTS verdict varchar(32) NOT NULL DEFAULT ''",
    "ALTER TABLE execution_rule_evaluation "
    "ADD COLUMN IF NOT EXISTS live_result jsonb NULL",
    "ALTER TABLE execution_rule_evaluation "
    "ADD COLUMN IF NOT EXISTS overridden boolean NOT NULL DEFAULT false",
    "ALTER TABLE execution_rule_evaluation "
    "ADD COLUMN IF NOT EXISTS injected_context jsonb NULL",
]


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0011_merge_20260626_1215"),
    ]

    operations = [
        migrations.RunSQL(sql=stmt, reverse_sql=migrations.RunSQL.noop)
        for stmt in _ADD_COLUMNS
    ]
