"""Add narrative_context fields to AuditSop and AuditStep.

Holds LLM-generated story text written by the narrative agent
(uhc_sop_ingestion.agents.a17_narrative) after graph extraction.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sop_ingestion", "0006_ingestionjob_workflow"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditsop",
            name="narrative_context",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="auditstep",
            name="narrative_context",
            field=models.TextField(blank=True, default=""),
        ),
    ]
