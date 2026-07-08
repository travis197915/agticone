import logging
import os
import sys

from celery import Celery
from celery.signals import worker_ready

_log = logging.getLogger("sop_backend.startup")

# Celery's worker heartbeat reports os.getloadavg() to the broker. On some
# macOS / container setups that syscall raises OSError("Load averages are
# unobtainable"), which otherwise puts the worker in a reconnect crash loop.
if hasattr(os, "getloadavg"):
    _real_getloadavg = os.getloadavg

    def _safe_getloadavg():
        try:
            return _real_getloadavg()
        except OSError:
            return (0.0, 0.0, 0.0)

    os.getloadavg = _safe_getloadavg  # type: ignore[attr-defined]

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sop_backend.settings")

# Celery's default prefork pool is unreliable on Windows (billiard task registry
# fails → "not enough values to unpack (expected 3, got 0)" in fast_trace_task).
if sys.platform == "win32":
    os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")

app = Celery("sop_backend")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


# ── Startup datastore connectivity check ──────────────────────────────────────
# On worker boot, probe every backing store the ingestion/execution pipelines
# depend on (Postgres, Redis, MongoDB, Neo4j) and log a clear OK/FAIL line each.
# Purely diagnostic and fully defensive — a probe failure is logged, never
# raised, so a degraded store can't stop the worker from starting.

def _redact_uri(uri: str) -> str:
    """Hide the password in a connection URI before logging it."""
    import re
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", uri)

def _check_postgres() -> tuple[bool, str]:
    from django.db import connections
    conn = connections["default"]
    conn.ensure_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    s = conn.settings_dict
    return True, f"{s.get('HOST') or 'localhost'}:{s.get('PORT') or 5432}/{s.get('NAME')}"


def _check_redis(cfg) -> tuple[bool, str]:
    from uhc_sop_ingestion.config import get_redis
    get_redis(cfg).ping()
    tls = " tls" if getattr(cfg, "redis_ssl_kwargs", {}) else ""
    return True, f"{cfg.redis_host}:{cfg.redis_port}{tls}"


def _redact_uri(uri: str) -> str:
    """Hide the password in a connection URI before logging it."""
    import re
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", uri)


def _check_mongo(cfg) -> tuple[bool, str]:
    from uhc_sop_ingestion.config import get_mongo

    from sop_backend.db_config import mongo_uri_from_env
    get_mongo(cfg).admin.command("ping")
    return True, f"{_redact_uri(mongo_uri_from_env())} ({cfg.mongo_database})"


def _check_neo4j(cfg) -> tuple[bool, str]:
    from uhc_sop_ingestion.config import get_neo4j

    from sop_backend.db_config import neo4j_uri_from_env
    get_neo4j(cfg).verify_connectivity()
    return True, neo4j_uri_from_env()


def _log_datastore_connectivity() -> None:
    try:
        import django
        django.setup()
    except Exception:
        pass

    try:
        from uhc_sop_ingestion.config import PipelineConfig
        cfg = PipelineConfig.from_env()
    except Exception as exc:
        _log.warning("startup db-check: could not load PipelineConfig: %s", exc)
        cfg = None

    checks = [("Postgres", lambda: _check_postgres())]
    if cfg is not None:
        checks += [
            ("Redis", lambda: _check_redis(cfg)),
            ("MongoDB", lambda: _check_mongo(cfg)),
            ("Neo4j", lambda: _check_neo4j(cfg)),
        ]

    _log.info("── datastore connectivity check ──────────────────────────────")
    ok_count = 0
    for name, probe in checks:
        try:
            _, detail = probe()
            ok_count += 1
            _log.info("  [ OK ] %-9s connected  (%s)", name, detail)
        except Exception as exc:
            _log.error("  [FAIL] %-9s NOT connected — %s: %s",
                       name, type(exc).__name__, exc)
    _log.info("── datastores: %d/%d connected ───────────────────────────────",
              ok_count, len(checks))


@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs):
    _log_datastore_connectivity()
