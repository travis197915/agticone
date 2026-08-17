import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Hand-written, minimal: adds only workflow_version_snapshot.

    Deliberately NOT auto-generated — `makemigrations` bundles pre-existing,
    unrelated model/migration drift on this branch (claimmemory unique_together,
    auditor_status alteration, RuleExecutionRunFieldChange re-creation) that
    predates this feature. Same approach already used for migration 0021.
    """

    dependencies = [
        ("execution_app", "0021_ruleexecutionrun_version_snapshot"),
        ("builder", "0007_workflow_version_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="ruleexecutionrun",
            name="workflow_version_snapshot",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="execution_runs", to="builder.workflowversion",
            ),
        ),
    ]
