"""Health check — ping every backing component and report status.

Public (no auth — a load balancer / uptime monitor can't carry a JWT), plain
Django view so DRF's global ``IsAuthenticated`` default never applies here.

GET /api/health/
    {
      "status": "ok" | "degraded",
      "timestamp": "...",
      "components": {
        "postgres": {"status": "ok", "detail": "...", "latency_ms": 4.2},
        "redis":    {"status": "ok", "detail": "...", "latency_ms": 1.1},
        "mongodb":  {"status": "fail", "error": "...", "latency_ms": 5002.3},
        "neo4j":    {"status": "ok", "detail": "...", "latency_ms": 8.7},
        "rabbitmq": {"status": "ok", "detail": "...", "latency_ms": 3.4}
      }
    }

200 when every component is ok, 503 when any of them failed.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

from django.http import JsonResponse
from django.utils import timezone
from django.views import View


# scheme://[creds@]host[:port][/rest] — captures just the scheme and
# whatever trails the host (path/db name), so masking can drop the middle.
_URI_RE = re.compile(r"^(?P<scheme>[\w+]+://)(?:[^@/]+@)?[^/]+(?P<rest>/.*)?$")


def _mask(uri: str) -> str:
    """Hide host/port (and any credentials) in a connection string.

    This is a public, unauthenticated endpoint — the response must not leak
    internal hostnames/ports, only enough to confirm which store/db answered.
    ``redis://user:pass@prod-redis.internal:6379/0`` -> ``redis://***/0``.
    """
    m = _URI_RE.match(uri)
    if not m:
        return "***"
    return f"{m.group('scheme')}***{m.group('rest') or ''}"


def _timed(check: Callable[[], str]) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        detail = check()
        return {
            "status": "ok",
            "detail": detail,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
        }
    except Exception as exc:
        return {
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
        }


def _check_postgres() -> str:
    from django.db import connections
    conn = connections["default"]
    conn.ensure_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    return f"postgres://***/{conn.settings_dict.get('NAME')}"


def _check_redis(cfg) -> str:
    from uhc_sop_ingestion.config import get_redis
    get_redis(cfg).ping()
    return "redis://***/0"


def _check_mongo(cfg) -> str:
    from uhc_sop_ingestion.config import get_mongo
    get_mongo(cfg).admin.command("ping")
    return f"mongodb://***/{cfg.mongo_database}"


def _check_neo4j(cfg) -> str:
    from uhc_sop_ingestion.config import get_neo4j
    get_neo4j(cfg).verify_connectivity()
    return _mask(cfg.neo4j_uri)


def _check_rabbitmq() -> str:
    import kombu
    from django.conf import settings
    broker_url = settings.CELERY_BROKER_URL
    with kombu.Connection(broker_url, connect_timeout=5) as conn:
        conn.ensure_connection(max_retries=1, timeout=5)
    return _mask(broker_url)


class HealthView(View):
    def get(self, request):
        components: dict[str, Any] = {"postgres": _timed(_check_postgres)}

        try:
            from uhc_sop_ingestion.config import PipelineConfig
            cfg = PipelineConfig.from_env()
        except Exception as exc:
            cfg = None
            config_error = f"{type(exc).__name__}: {exc}"
            components["redis"] = {"status": "fail", "error": config_error}
            components["mongodb"] = {"status": "fail", "error": config_error}
            components["neo4j"] = {"status": "fail", "error": config_error}

        if cfg is not None:
            components["redis"] = _timed(lambda: _check_redis(cfg))
            components["mongodb"] = _timed(lambda: _check_mongo(cfg))
            components["neo4j"] = _timed(lambda: _check_neo4j(cfg))

        components["rabbitmq"] = _timed(_check_rabbitmq)

        all_ok = all(c["status"] == "ok" for c in components.values())
        payload = {
            "status": "ok" if all_ok else "degraded",
            "timestamp": timezone.now().isoformat(),
            "components": components,
        }
        return JsonResponse(payload, status=200 if all_ok else 503)
