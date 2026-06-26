"""Ensure ``execution_rule_evaluation.verdict`` exists on fresh databases.

``0002_ruleevaluation_verdict`` only updated Django state (production already
had the column). Test DBs and new installs need the physical column.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0005_ruleexecutionrun_review_status"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "ALTER TABLE execution_rule_evaluation "
                "ADD COLUMN IF NOT EXISTS verdict varchar(32) NOT NULL DEFAULT '';"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
