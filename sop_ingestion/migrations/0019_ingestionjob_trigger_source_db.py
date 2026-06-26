"""Add the ``trigger_source`` COLUMN to the database, idempotently.

Background — two environments, two different schema states:

* On the originally-targeted (remote) DB the ``trigger_source`` column already
  existed (NOT NULL, no default), so migration 0018 reconciled it *state-only*.
* On this (local) DB the column was never created, so 0018 was a no-op at the
  database level and every insert failed with
  ``UndefinedColumn: column "trigger_source" ... does not exist``.

This migration adds the column with ``ADD COLUMN IF NOT EXISTS`` so it is safe
on BOTH databases: a no-op where the column already exists, and an additive
``ADD COLUMN`` where it is missing. The model field (and Django state) was
already declared by 0018, so this migration carries **database operations
only** — no state change — to avoid a duplicate field in the project state.

The default (``'api'``) backfills any pre-existing rows and matches the model's
app-level default, so the column is NOT NULL without a separate backfill step.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0016_widen_ingesteddocument_sop_ids"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE sop_ingestion_ingestionjob "
                        "ADD COLUMN IF NOT EXISTS trigger_source "
                        "varchar(32) NOT NULL DEFAULT 'api';"
                    ),
                    reverse_sql=(
                        "ALTER TABLE sop_ingestion_ingestionjob "
                        "DROP COLUMN IF EXISTS trigger_source;"
                    ),
                ),
            ],
        ),
    ]
