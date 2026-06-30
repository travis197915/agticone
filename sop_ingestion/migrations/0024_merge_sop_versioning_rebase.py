"""Merge sop_ingestion branches after sop-versioning rebase."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0017_merge_20260615_0552"),
        ("sop_ingestion", "0023_version_revision_db_defaults"),
    ]

    operations = []
