"""
Migration 0005 — Materialised Knowledge Graph

Adds two tables that hold the SOP knowledge-graph as first-class
nodes and edges, mirrored from the audit_* business tables:

  sop_ingestion_auditgraphnode  — one row per graph node (DOCUMENT, STEP,
                                  DECISION, PRE_RULE, CODE, REFERENCE, …)
  sop_ingestion_auditgraphedge  — one row per directed edge with a typed
                                  relationship (HAS_STEP, HAS_DECISION,
                                  GOTO, REFERENCES, …)

The same graph is also pushed to Neo4j by a10_write_neo4j.py — these
tables let downstream consumers query the graph from SQL without
hitting Neo4j and serve as the source of truth for the Cytoscape viewer.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0004_claims_audit_schema"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE TABLE IF NOT EXISTS sop_ingestion_auditgraphnode (
                    id              SERIAL PRIMARY KEY,
                    sop_id          INTEGER NOT NULL
                                      REFERENCES sop_ingestion_auditsop(id)
                                      ON DELETE CASCADE,
                    node_key        VARCHAR(128) NOT NULL,
                    node_type       VARCHAR(24)  NOT NULL,
                    label           VARCHAR(255) NOT NULL,
                    details         JSONB NOT NULL DEFAULT '{}'::jsonb,
                    ref_table       VARCHAR(64)  NOT NULL DEFAULT '',
                    ref_id          INTEGER,
                    display_order   INTEGER NOT NULL DEFAULT 0,
                    CONSTRAINT auditgraphnode_sop_key_uniq UNIQUE (sop_id, node_key)
                );
                CREATE INDEX IF NOT EXISTS auditgraphnode_sop_idx
                    ON sop_ingestion_auditgraphnode(sop_id);
                CREATE INDEX IF NOT EXISTS auditgraphnode_sop_type_idx
                    ON sop_ingestion_auditgraphnode(sop_id, node_type);
                CREATE INDEX IF NOT EXISTS auditgraphnode_key_idx
                    ON sop_ingestion_auditgraphnode(node_key);

                CREATE TABLE IF NOT EXISTS sop_ingestion_auditgraphedge (
                    id          SERIAL PRIMARY KEY,
                    sop_id      INTEGER NOT NULL
                                  REFERENCES sop_ingestion_auditsop(id)
                                  ON DELETE CASCADE,
                    source_id   INTEGER NOT NULL
                                  REFERENCES sop_ingestion_auditgraphnode(id)
                                  ON DELETE CASCADE,
                    target_id   INTEGER NOT NULL
                                  REFERENCES sop_ingestion_auditgraphnode(id)
                                  ON DELETE CASCADE,
                    rel_type    VARCHAR(24)  NOT NULL,
                    label       VARCHAR(255) NOT NULL DEFAULT '',
                    details     JSONB NOT NULL DEFAULT '{}'::jsonb
                );
                CREATE INDEX IF NOT EXISTS auditgraphedge_sop_idx
                    ON sop_ingestion_auditgraphedge(sop_id);
                CREATE INDEX IF NOT EXISTS auditgraphedge_sop_rel_idx
                    ON sop_ingestion_auditgraphedge(sop_id, rel_type);
                CREATE INDEX IF NOT EXISTS auditgraphedge_source_rel_idx
                    ON sop_ingestion_auditgraphedge(source_id, rel_type);
                CREATE INDEX IF NOT EXISTS auditgraphedge_target_idx
                    ON sop_ingestion_auditgraphedge(target_id);
            """,
            reverse_sql="""
                DROP TABLE IF EXISTS sop_ingestion_auditgraphedge CASCADE;
                DROP TABLE IF EXISTS sop_ingestion_auditgraphnode CASCADE;
            """,
        ),
        # State-only operations: tell Django the models exist so it doesn't
        # try to re-create them.
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.CreateModel(
                    name="AuditGraphNode",
                    fields=[
                        ("id", models.AutoField(primary_key=True, auto_created=True,
                                                serialize=False, verbose_name="ID")),
                        ("node_key", models.CharField(max_length=128, db_index=True)),
                        ("node_type", models.CharField(max_length=24, db_index=True)),
                        ("label", models.CharField(max_length=255)),
                        ("details", models.JSONField(default=dict, blank=True)),
                        ("ref_table", models.CharField(max_length=64, blank=True)),
                        ("ref_id", models.PositiveIntegerField(null=True, blank=True)),
                        ("display_order", models.PositiveIntegerField(default=0)),
                        ("sop", models.ForeignKey(
                            on_delete=django.db.models.deletion.CASCADE,
                            related_name="graph_nodes",
                            to="sop_ingestion.auditsop")),
                    ],
                    options={
                        "verbose_name": "Audit Graph Node",
                        "unique_together": {("sop", "node_key")},
                    },
                ),
                migrations.CreateModel(
                    name="AuditGraphEdge",
                    fields=[
                        ("id", models.AutoField(primary_key=True, auto_created=True,
                                                serialize=False, verbose_name="ID")),
                        ("rel_type", models.CharField(max_length=24, db_index=True)),
                        ("label", models.CharField(max_length=255, blank=True)),
                        ("details", models.JSONField(default=dict, blank=True)),
                        ("sop", models.ForeignKey(
                            on_delete=django.db.models.deletion.CASCADE,
                            related_name="graph_edges",
                            to="sop_ingestion.auditsop")),
                        ("source", models.ForeignKey(
                            on_delete=django.db.models.deletion.CASCADE,
                            related_name="edges_out",
                            to="sop_ingestion.auditgraphnode")),
                        ("target", models.ForeignKey(
                            on_delete=django.db.models.deletion.CASCADE,
                            related_name="edges_in",
                            to="sop_ingestion.auditgraphnode")),
                    ],
                    options={"verbose_name": "Audit Graph Edge"},
                ),
            ],
        ),
    ]
