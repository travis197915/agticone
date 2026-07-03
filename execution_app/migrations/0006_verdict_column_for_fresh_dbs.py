"""Materialize the `verdict` column on fresh databases.

0002 documented the column state-only because production had added it
out-of-band — but that left every FRESH database (notably the test runner's)
without the column, so any test inserting a RuleEvaluation failed with
UndefinedColumn. ``ADD COLUMN IF NOT EXISTS`` is a no-op where the column
already exists and creates it everywhere else. State is untouched (0002
already added the field to Django's model state).
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0005_claimmemory_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql=("ALTER TABLE execution_rule_evaluation "
                 "ADD COLUMN IF NOT EXISTS verdict varchar(32) "
                 "NOT NULL DEFAULT ''"),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
