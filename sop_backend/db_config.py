"""Project-wide datastore connection helpers.

Single source of truth for building datastore connection URIs from the
environment, shared by EVERY part of the project (Django views, management
commands, the Celery startup health check, etc.). Lives next to ``settings.py``
so it is a project-level concern, not tied to any one app or package.

Environment gating
-------------------
The managed/prod connection strings (Atlas Mongo, TLS Neo4j) are used ONLY when
``APP_ENV`` marks the environment as production. In every other environment
(local dev, CI) the plain local ``host:port`` build is used, even if a prod URI
happens to be present in the environment — so a stray ``MONGO_URI`` can never
point local dev at the prod cluster.
"""
from __future__ import annotations

import os
from urllib.parse import quote_plus

_PROD_VALUES = {"prod", "production"}
_TRUTHY = {"1", "true", "yes", "on"}


def is_prod() -> bool:
    """True when ``APP_ENV`` marks this as a production environment.

    Set ``APP_ENV=prod`` (or ``production``) in the prod environment; leave it
    unset / anything else for local dev, CI, staging, etc.
    """
    return os.environ.get("APP_ENV", "").strip().lower() in _PROD_VALUES


def _truthy(val: str) -> bool:
    return val.strip().lower() in _TRUTHY


# ── Redis ─────────────────────────────────────────────────────────────────────

def redis_ssl_enabled() -> bool:
    """True when Redis should connect over TLS — PROD ONLY.

    Managed Redis (Azure) requires TLS; local dev Redis does not. Enabled only
    when ``APP_ENV=prod`` and ``REDIS_SSL`` is truthy (defaults on in prod).
    """
    return is_prod() and _truthy(os.environ.get("REDIS_SSL", "true"))


def redis_ssl_kwargs() -> dict:
    """SSL kwargs for a direct ``redis.Redis(...)`` client — empty in non-prod.

    Azure Redis terminates TLS with a cert that won't pass hostname/CA checks
    over the private link, so verification is relaxed (matches the managed
    endpoint's requirements); tune via ``REDIS_SSL_CHECK_HOSTNAME``.
    """
    if not redis_ssl_enabled():
        return {}
    return {
        "ssl": True,
        "ssl_check_hostname": _truthy(os.environ.get("REDIS_SSL_CHECK_HOSTNAME", "false")),
        "ssl_cert_reqs": None,
    }


def redis_url_from_env() -> str:
    """Resolve the Redis URL from the environment (for ``redis.from_url``).

    - **prod + TLS**: ``rediss://…?ssl_cert_reqs=none`` (Azure managed Redis).
    - **otherwise**: plain ``redis://…``.
    """
    user = os.environ.get("REDIS_USER", "default")
    password = os.environ.get("REDIS_PASSWORD", "")
    host = os.environ.get("REDIS_HOST", "localhost")
    port = os.environ.get("REDIS_PORT", "6379")
    auth = f"{quote_plus(user)}:{quote_plus(password)}@" if password else ""
    if redis_ssl_enabled():
        check = "true" if _truthy(os.environ.get("REDIS_SSL_CHECK_HOSTNAME", "false")) else "false"
        return f"rediss://{auth}{host}:{port}/0?ssl_cert_reqs=none&ssl_check_hostname={check}"
    return f"redis://{auth}{host}:{port}/0"


def _local_mongo_uri() -> str:
    host = os.environ.get("MONGO_HOST", "localhost")
    port = os.environ.get("MONGO_PORT", "27017")
    user = os.environ.get("MONGO_USER", "")
    password = os.environ.get("MONGO_PASSWORD", "")
    # Omit "user:pass@" when either is empty — an auth-less local Mongo
    # (docker) rejects "mongodb://:@host".
    creds = f"{quote_plus(user)}:{quote_plus(password)}@" if (user and password) else ""
    return f"mongodb://{creds}{host}:{port}/"


def mongo_uri_from_env() -> str:
    """Resolve the MongoDB connection URI from the environment.

    - **prod** (``APP_ENV=prod``): use ``MONGO_URI`` — the full connection
      string, typically Atlas (``mongodb+srv://user:pass@cluster.mongodb.net/?...``).
      Falls back to the local host/port build only if ``MONGO_URI`` is unset.
    - **non-prod**: always the local ``mongodb://host:port`` build; ``MONGO_URI``
      is ignored so local/CI can never dial the prod cluster.
    """
    if is_prod():
        uri = os.environ.get("MONGO_URI", "").strip()
        if uri:
            return uri
    return _local_mongo_uri()


def neo4j_uri_from_env() -> str:
    """Resolve the Neo4j connection URI from the environment.
 
    - **prod** (``APP_ENV=prod``): use ``NEO4J_URI`` (full URI) if set, else
      ``NEO4J_SCHEME`` + ``NEO4J_HOST``/``NEO4J_PORT``. The prod default scheme
      is ``bolt+ssc`` — a DIRECT connection with self-signed-cert TLS. Do NOT
      default to a routing scheme (``neo4j`` / ``neo4j+ssc``): against a
      single-instance server that raises "Unable to retrieve routing
      information". Set ``NEO4J_SCHEME=neo4j+ssc`` explicitly only for a real
      cluster / Aura endpoint.
    - **non-prod**: always the plain ``neo4j://host:port`` scheme; the prod
      URI/scheme overrides are ignored.
    """
    host = os.environ.get("NEO4J_HOST", "localhost")
    port = os.environ.get("NEO4J_PORT", "7687")
    if is_prod():
        uri = os.environ.get("NEO4J_URI", "").strip()
        if uri:
            return uri
        scheme = (os.environ.get("NEO4J_SCHEME", "").strip() or "bolt+ssc")
        return f"{scheme}://{host}:{port}"
    return f"neo4j://{host}:{port}"
