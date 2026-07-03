"""Rescope ClaimMemory from (claim_id, workflow) to (claim_id, sop_id).

Existing rows are cleared: they were keyed per workflow and cannot be split
into per-SOP rows (RuleEvaluation does not persist a sop column), and memory
regenerates organically from the next run of each claim. Operation order
matters — the old unique constraint must drop before the workflow column
does, and the new one can only be added after sop_id exists.
"""
from django.db import migrations, models


def _clear_rows(apps, schema_editor):
    apps.get_model("execution_app", "ClaimMemory").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0006_verdict_column_for_fresh_dbs"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="claimmemory",
            unique_together=set(),
        ),
        migrations.RunPython(_clear_rows, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="claimmemory",
            name="workflow",
        ),
        migrations.AddField(
            model_name="claimmemory",
            name="sop_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="claimmemory",
            name="sop_title",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
        migrations.AlterUniqueTogether(
            name="claimmemory",
            unique_together={("claim_id", "sop_id")},
        ),
    ]
