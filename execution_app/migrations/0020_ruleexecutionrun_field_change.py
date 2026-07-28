from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("execution_app", "0019_corebackenduser"),
    ]

    operations = [
        migrations.CreateModel(
            name="RuleExecutionRunFieldChange",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("field_name", models.CharField(max_length=64)),
                ("old_value", models.JSONField(blank=True, null=True)),
                ("new_value", models.JSONField(blank=True, null=True)),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                ("changed_by", models.CharField(blank=True, default="", max_length=255)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="field_changes",
                        to="execution_app.ruleexecutionrun",
                    ),
                ),
            ],
            options={
                "db_table": "execution_rule_run_field_change",
                "ordering": ["-changed_at"],
            },
        ),
    ]
