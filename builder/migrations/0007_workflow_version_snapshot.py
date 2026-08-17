import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('builder', '0006_workbench_is_current_workbench_version_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='WorkflowVersion',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('version_number', models.PositiveIntegerField()),
                ('reason', models.CharField(blank=True, default='', max_length=64)),
                ('workflow', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='versions', to='builder.workflow')),
            ],
            options={
                'db_table': 'builder_workflow_version',
                'ordering': ['workflow', 'version_number'],
            },
        ),
        migrations.CreateModel(
            name='WorkflowVersionWorkbench',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('node_key', models.CharField(blank=True, default='', max_length=128)),
                ('order', models.PositiveIntegerField(default=0)),
                ('workbench_version', models.PositiveIntegerField()),
                ('audit_sop_id', models.PositiveIntegerField(blank=True, null=True)),
                ('sop_title', models.CharField(blank=True, default='', max_length=255)),
                ('sop_version_number', models.PositiveIntegerField(blank=True, null=True)),
                ('workbench', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='version_snapshots', to='builder.workbench')),
                ('workflow_version', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slots', to='builder.workflowversion')),
            ],
            options={
                'db_table': 'builder_workflow_version_workbench',
            },
        ),
        migrations.AddConstraint(
            model_name='workflowversion',
            constraint=models.UniqueConstraint(fields=('workflow', 'version_number'), name='uniq_workflow_version_number'),
        ),
        migrations.AddConstraint(
            model_name='workflowversionworkbench',
            constraint=models.UniqueConstraint(fields=('workflow_version', 'workbench'), name='uniq_wfv_workbench'),
        ),
        migrations.AddConstraint(
            model_name='workflowversionworkbench',
            constraint=models.UniqueConstraint(fields=('workflow_version', 'node_key'), name='uniq_wfv_node_key'),
        ),
    ]
