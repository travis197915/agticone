"""Add AuditDecision.applicable_when for routing-aware execution.

Hand-written (matching the 0012 style) so it stays additive and does not pull
in unrelated ORM/schema drift. The column is nullable-by-default text, so
existing rows keep working (applicable_when='').
"""
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0012_audit_decision_nesting"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditdecision",
            name="applicable_when",
            field=models.TextField(blank=True, default=""),
        ),
    ]
