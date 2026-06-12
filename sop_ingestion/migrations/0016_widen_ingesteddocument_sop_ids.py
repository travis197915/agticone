"""Widen IngestedDocument.neo4j_sop_id / pg_sop_id from 128 → 512.

Hand-written (focused) on purpose. ``makemigrations`` wanted to bundle a large
amount of pre-existing model/migration drift (legacy Ctx*/Sop* model deletions,
BigAutoField id alters, and removal of named unique constraints the ingestion
pipeline's ``ON CONFLICT ON CONSTRAINT`` SQL still depends on). That drift
predates this change and is intentionally NOT carried here so this migration is
safe to apply in isolation.

A PDF ingested from a local upload produces a long ``file://…`` provenance id
that overflowed the old varchar(128), causing a non-fatal "value too long for
type character varying(128)" warning when saving the document tracking row.
Increasing a varchar length is a Postgres catalog-only change (no table rewrite).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0015_sopirdocument"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ingesteddocument",
            name="neo4j_sop_id",
            field=models.CharField(blank=True, max_length=512),
        ),
        migrations.AlterField(
            model_name="ingesteddocument",
            name="pg_sop_id",
            field=models.CharField(blank=True, max_length=512),
        ),
    ]
