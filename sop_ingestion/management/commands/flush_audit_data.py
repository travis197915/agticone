"""Flush operational data from every datastore for a clean build.

Wipes the *operational* data the SOP-ingestion + workflow-builder + execution
flows produce, across all four stores configured in ``.env``:

  * Postgres  — execution runs, builder workflows (+ areas/benches/shapes/
                connections/bindings), ingestion jobs (+ audit SOP/step/
                decision rows and pipeline/LLM logs).
  * MongoDB   — every collection in ``MONGO_DATABASE``.
  * Neo4j     — every node + relationship in ``NEO4J_DATABASE``.
  * Redis     — ``FLUSHDB`` on the configured DB index.

The server-driven *catalog* (ShapeCategory / ShapeDefinition / NavItem /
DashboardWidget) and the agent_tools ``Tool`` registry are PRESERVED by
default, because the builder palette and tool picker depend on them and the
auto-build path needs the ``rectangle`` / ``diamond`` / ``round-rectangle``
definitions. Pass ``--include-catalog`` to wipe those too.

This command is destructive and irreversible. It requires ``--yes``.

Usage::

    PYTHONPATH=. python manage.py flush_audit_data --yes
    PYTHONPATH=. python manage.py flush_audit_data --yes --include-catalog
    PYTHONPATH=. python manage.py flush_audit_data --yes --skip-mongo --skip-neo4j
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Delete operational data from Postgres/Mongo/Neo4j/Redis for a clean build."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true",
                            help="Confirm the destructive wipe (required).")
        parser.add_argument("--include-catalog", action="store_true",
                            help="Also delete the builder catalog + tool registry.")
        parser.add_argument("--skip-postgres", action="store_true")
        parser.add_argument("--skip-mongo", action="store_true")
        parser.add_argument("--skip-neo4j", action="store_true")
        parser.add_argument("--skip-redis", action="store_true")

    def handle(self, *args, **opts):
        if not opts["yes"]:
            self.stderr.write(self.style.ERROR(
                "Refusing to flush without --yes. This is destructive and "
                "irreversible."))
            return

        if not opts["skip_postgres"]:
            self._flush_postgres(include_catalog=opts["include_catalog"])
        if not opts["skip_mongo"]:
            self._flush_mongo()
        if not opts["skip_neo4j"]:
            self._flush_neo4j()
        if not opts["skip_redis"]:
            self._flush_redis()

        self.stdout.write(self.style.SUCCESS("\nFlush complete."))

    # ── Postgres (Django ORM) ────────────────────────────────────────────────
    def _flush_postgres(self, *, include_catalog: bool) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("Postgres…"))

        # 1) Execution traces first — BatchExecutionRun.workflow is PROTECT, so
        #    these must go before the workflows they reference.
        try:
            from execution_app.models import (BatchExecutionRun,
                                              RuleExecutionRun)
            n_batch, _ = BatchExecutionRun.objects.all().delete()
            n_run, _ = RuleExecutionRun.objects.all().delete()
            self.stdout.write(f"  execution runs: {n_batch + n_run} rows")
        except Exception as exc:  # pragma: no cover - best effort
            self.stderr.write(self.style.WARNING(f"  execution_app skip: {exc}"))

        # 2) Builder workflows — cascades WorkArea/Workbench/Shape/Connection
        #    and the agent_tools bindings hung off each Shape.
        try:
            from builder.models import Workflow
            n_wf, _ = Workflow.objects.all().delete()
            self.stdout.write(f"  workflows (+ cascade): {n_wf} rows")
        except Exception as exc:  # pragma: no cover
            self.stderr.write(self.style.WARNING(f"  builder skip: {exc}"))

        # Any bindings not cascaded (defensive — should be none left).
        try:
            from agent_tools.models import NodeRuleBinding, NodeToolBinding
            NodeToolBinding.objects.all().delete()
            NodeRuleBinding.objects.all().delete()
        except Exception as exc:  # pragma: no cover
            self.stderr.write(self.style.WARNING(f"  bindings skip: {exc}"))

        # 3) Ingestion jobs — cascades AuditSop/Step/Decision + all audit child
        #    tables, IngestedDocument, PipelineStageLog, LLMCallLog.
        try:
            from sop_ingestion.models import IngestionJob
            n_job, _ = IngestionJob.objects.all().delete()
            self.stdout.write(f"  ingestion jobs (+ cascade): {n_job} rows")
        except Exception as exc:  # pragma: no cover
            self.stderr.write(self.style.WARNING(f"  sop_ingestion skip: {exc}"))

        if include_catalog:
            try:
                from agent_tools.models import Tool
                from builder.models import (DashboardWidget, NavItem,
                                            ShapeCategory, ShapeDefinition)
                Tool.objects.all().delete()
                DashboardWidget.objects.all().delete()
                NavItem.objects.all().delete()
                ShapeDefinition.objects.all().delete()
                ShapeCategory.objects.all().delete()
                self.stdout.write("  catalog + tools: wiped")
            except Exception as exc:  # pragma: no cover
                self.stderr.write(self.style.WARNING(f"  catalog skip: {exc}"))
        else:
            self.stdout.write("  catalog + tools: preserved (use "
                              "--include-catalog to wipe)")

    # ── MongoDB ──────────────────────────────────────────────────────────────
    def _flush_mongo(self) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("MongoDB…"))
        try:
            from pymongo import MongoClient
            from urllib.parse import quote_plus

            host = os.environ.get("MONGO_HOST", "localhost")
            port = int(os.environ.get("MONGO_PORT", "27017"))
            user = os.environ.get("MONGO_USER", "")
            pw = os.environ.get("MONGO_PASSWORD", "")
            db_name = os.environ.get("MONGO_DATABASE", "sop_ingestion")
            if user and pw:
                creds = f"{quote_plus(user)}:{quote_plus(pw)}@"
            else:
                creds = ""
            uri = f"mongodb://{creds}{host}:{port}/"
            client = MongoClient(uri, serverSelectionTimeoutMS=10000)
            db = client[db_name]
            cols = db.list_collection_names()
            for c in cols:
                db.drop_collection(c)
            self.stdout.write(f"  dropped {len(cols)} collection(s) in {db_name}")
            client.close()
        except Exception as exc:
            self.stderr.write(self.style.WARNING(f"  mongo skip: {exc}"))

    # ── Neo4j ────────────────────────────────────────────────────────────────
    def _flush_neo4j(self) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("Neo4j…"))
        try:
            from neo4j import GraphDatabase

            host = os.environ.get("NEO4J_HOST", "localhost")
            port = int(os.environ.get("NEO4J_PORT", "7687"))
            user = os.environ.get("NEO4J_USER", "neo4j")
            pw = os.environ.get("NEO4J_PASSWORD", "")
            db_name = os.environ.get("NEO4J_DATABASE", "neo4j")
            driver = GraphDatabase.driver(f"neo4j://{host}:{port}", auth=(user, pw))
            with driver.session(database=db_name) as session:
                session.run("MATCH (n) DETACH DELETE n")
            driver.close()
            self.stdout.write(f"  detach-deleted all nodes in {db_name}")
        except Exception as exc:
            self.stderr.write(self.style.WARNING(f"  neo4j skip: {exc}"))

    # ── Redis ────────────────────────────────────────────────────────────────
    def _flush_redis(self) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("Redis…"))
        try:
            import redis

            host = os.environ.get("REDIS_HOST", "localhost")
            port = int(os.environ.get("REDIS_PORT", "6379"))
            user = os.environ.get("REDIS_USER", "default")
            pw = os.environ.get("REDIS_PASSWORD", "")
            db_idx = int(os.environ.get("REDIS_DB", "0"))
            client = redis.Redis(
                host=host, port=port, username=user, password=pw,
                db=db_idx, socket_connect_timeout=10,
            )
            client.flushdb()
            self.stdout.write(f"  flushed Redis DB {db_idx}")
        except Exception as exc:
            self.stderr.write(self.style.WARNING(f"  redis skip: {exc}"))
