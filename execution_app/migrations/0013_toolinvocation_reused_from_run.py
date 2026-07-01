from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0012_merge_20260628_claim_lob"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE execution_tool_invocation "
                        "ADD COLUMN IF NOT EXISTS reused_from_run uuid"
                    ),
                    reverse_sql=(
                        "ALTER TABLE execution_tool_invocation "
                        "DROP COLUMN IF EXISTS reused_from_run"
                    ),
                )
            ],
            state_operations=[
                migrations.AddField(
                    model_name="toolinvocationrecord",
                    name="reused_from_run",
                    field=models.UUIDField(blank=True, null=True),
                )
            ],
        ),
    ]
