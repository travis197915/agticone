"""Merge agent_tools branches after sop-versioning rebase."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("agent_tools", "0006_merge_20260615_0552"),
        ("agent_tools", "0011_cbdtoolconfig"),
    ]

    operations = []
