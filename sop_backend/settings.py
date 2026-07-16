"""
sop_backend/settings.py
-----------------------
Standalone Django project that imports the uhc-sop-ingestion pip module
and exposes the LangGraph pipeline as a REST API.

Credentials are read from:
  ../uhc-backend/.env   (PG_*, REDIS_*, NEO4J_*, MONGO_*, OPENAI_API_KEY …)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Load .env from project root ───────────────────────────────────────────────
ENV_FILE = BASE_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE, override=False)   # OS env always takes precedence

# Load agent_tools/.env.tools after the main .env so tool-level URLs are
# available before any tool module reads os.getenv at import time. The main
# .env values still win because override=False.
_TOOLS_ENV_FILE = BASE_DIR / "agent_tools" / ".env.tools"
if _TOOLS_ENV_FILE.exists():
    load_dotenv(_TOOLS_ENV_FILE, override=False)

# ── Azure Key Vault → os.environ (optional; matches reference ask_llm.py) ───
# Loads AUTH_URL, CLIENT_ID, CLIENT_SECRET, SCOPE, MODEL_REGISTRY_JSON, etc.
# from Key Vault when AZURE_KEY_VAULT_URL is set (or .env.stg provides KV creds).
try:
    from uhc_llm.keyvault_loader import bootstrap_llm_secrets, keyvault_configured

    if keyvault_configured() or (BASE_DIR / ".env.stg").exists():
        bootstrap_llm_secrets()
except Exception as _kv_exc:
    import logging
    logging.getLogger(__name__).warning("Key Vault bootstrap skipped: %s", _kv_exc)

# ── Validate LLM registry config early (Option A: MODEL_REGISTRY_JSON) ─────
if os.environ.get("LLM_BACKEND", "").strip().lower() == "registry":
    try:
        from uhc_llm.gateway import gateway_auth_configured, gateway_auth_source, GATEWAY_KEY_ENV_VARS
        from uhc_llm.oauth import OAUTH_ENV_VARS, oauth_configured
        from uhc_llm.registry import load_model_registry, registry_profile_name
        _llm_registry = load_model_registry()
        if not _llm_registry:
            import warnings
            warnings.warn(
                "LLM_BACKEND=registry but no models loaded. "
                "Set MODEL_REGISTRY_JSON in .env (see .env.example).",
                stacklevel=1,
            )
        elif not gateway_auth_configured():
            import warnings
            from uhc_llm.gateway import GATEWAY_KEY_ENV_VARS
            from uhc_llm.oauth import OAUTH_ENV_VARS
            warnings.warn(
                f"LLM registry loaded from {registry_profile_name()!r} "
                f"({len(_llm_registry)} models) but no gateway auth is set. "
                f"Set OAuth ({', '.join(OAUTH_ENV_VARS)}) or "
                f"API key ({', '.join(GATEWAY_KEY_ENV_VARS)}).",
                stacklevel=1,
            )
        else:
            import logging
            auth = gateway_auth_source()
            logging.getLogger(__name__).info("LLM registry auth via %s", auth)
            if not oauth_configured() and auth == "OPENAI_API_KEY":
                logging.getLogger(__name__).warning(
                    "LLM registry uses OPENAI_API_KEY for gateway auth. "
                    "api.uhg.com typically requires OAuth "
                    "(AUTH_URL, CLIENT_ID, CLIENT_SECRET, SCOPE)."
                )
    except Exception as _llm_exc:
        import warnings
        warnings.warn(f"LLM registry config invalid: {_llm_exc}", stacklevel=1)

# ── Core ──────────────────────────────────────────────────────────────────────
SECRET_KEY    = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-v2-dev-only")
DEBUG         = os.environ.get("DJANGO_DEBUG", "true").lower() in {"1", "true", "yes"}
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "sop_ingestion",
    "builder",
    "agent_tools",
    "execution_app",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# ── CORS ─────────────────────────────────────────────────────────────────────
# Browser origins allowed to call this backend. Dev defaults + whatever the
# CORS_ORIGINS env var provides, always unioned with the known prod frontends
# below (so a misconfigured/empty env var can never lock the deployed portals
# out).
CORS_ALLOWED_ORIGINS = [
    s.strip() for s in os.environ.get(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:5174,http://127.0.0.1:5174,"
        "http://localhost:5175,http://127.0.0.1:5175,"
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",") if s.strip()
]

# Deployed portals (dev). Always allowed regardless of the env var.
_PROD_CORS_ORIGINS = [
    "https://agenticai-backend-dev.optum.com",
    "https://claims-backend-dev.optum.com",
    "https://auditworkflow-claims-dev.optum.com",
    "https://audit-hitl-dev.optum.com",
    "https://audit-dashboard-dev.optum.com",
]
for _origin in _PROD_CORS_ORIGINS:
    if _origin not in CORS_ALLOWED_ORIGINS:
        CORS_ALLOWED_ORIGINS.append(_origin)
# In dev, Vite may pick any free port (5173, 5174, 5175, …). Allow any
# localhost/127.0.0.1 origin so the browser preflight passes regardless of the
# port the dev server landed on. Never enabled when DEBUG is off.
if DEBUG:
    CORS_ALLOWED_ORIGIN_REGEXES = [
        r"^http://localhost:\d+$",
        r"^http://127\.0\.0\.1:\d+$",
    ]
CORS_ALLOW_CREDENTIALS = True

ROOT_URLCONF      = "sop_backend.urls"
WSGI_APPLICATION  = "sop_backend.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.debug",
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

# ── Database ──────────────────────────────────────────────────────────────────
DATABASES = {
    "default": {
        "ENGINE":   "django.db.backends.postgresql",
        "NAME":     os.environ.get("PG_DATABASE"),
        "USER":     os.environ.get("PG_USER"),
        "PASSWORD": os.environ.get("PG_PASSWORD"),
        "HOST":     os.environ.get("PG_HOST"),
        "PORT":     os.environ.get("PG_PORT"),
        # Include the dedicated agent_tools schema on the connection's
        # search_path. Django's introspection (used by `migrate`, test
        # database flush, and `inspectdb`) only walks the search_path,
        # so without this it would miss agent_tools.tool /
        # node_rule_binding / node_tool_binding and refuse to TRUNCATE
        # sop_ingestion_auditsop (which has an FK target inside the
        # agent_tools schema).
        "OPTIONS": {
            "options": "-c search_path=public,agent_tools",
        },
    }
}

# claims-corebackend's user table normally lives on this same Postgres
# instance under a different schema (see execution_app.models.CorebackendUser
# / COREBACKEND_DB_SCHEMA). When corebackend is actually a separate Postgres
# instance, set COREBACKEND_DB_HOST (+ port/name/user/password) to register a
# second connection here — sop_backend.db_routers.CorebackendRouter then
# routes CorebackendUser reads to it automatically. Leave COREBACKEND_DB_HOST
# unset to keep the same-instance/different-schema behavior (the default).
_COREBACKEND_DB_HOST = os.environ.get("COREBACKEND_DB_HOST", "").strip()
if _COREBACKEND_DB_HOST:
    if not os.environ.get("COREBACKEND_DB_NAME", "").strip():
        raise ImproperlyConfigured(
            "COREBACKEND_DB_HOST is set but COREBACKEND_DB_NAME is not — "
            "the corebackend Postgres connection needs a database name."
        )
    DATABASES["corebackend"] = {
        "ENGINE":   "django.db.backends.postgresql",
        "NAME":     os.environ.get("COREBACKEND_DB_NAME", ""),
        "USER":     os.environ.get("COREBACKEND_DB_USER", ""),
        "PASSWORD": os.environ.get("COREBACKEND_DB_PASSWORD", ""),
        "HOST":     _COREBACKEND_DB_HOST,
        "PORT":     os.environ.get("COREBACKEND_DB_PORT", "5432"),
        # A genuinely separate instance being unreachable (not just refusing
        # the connection) must fail fast, not hang the claims-list request on
        # the OS TCP timeout — reviewer-name lookups already degrade to the
        # raw userID on any DatabaseError (see reviewer_lookup.py).
        "OPTIONS": {"connect_timeout": 5},
    }

DATABASE_ROUTERS = ["sop_backend.db_routers.CorebackendRouter"]

# ── Auth / i18n ───────────────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE      = "en-us"
TIME_ZONE          = "UTC"
USE_I18N           = True
USE_TZ             = True
STATIC_URL         = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── DRF — trust JWTs minted by the Node corebackend ─────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "builder.auth.CorebackendJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    # No pagination on builder catalog endpoints — the SPA wants a flat list.
}

# ── Redis ─────────────────────────────────────────────────────────────────────
# Prod (APP_ENV=prod) yields a rediss:// URL for the managed TLS endpoint
# (Azure); non-prod stays on plain redis://. See sop_backend/db_config.py.
from sop_backend.db_config import redis_url_from_env  # noqa: E402
REDIS_URL = redis_url_from_env()

# Export the composed URL back to the process environment so standalone
# packages that follow the standard `REDIS_URL` convention (notably
# `uhc_execution_engine.llm._get_redis`, which powers the SSE pub/sub
# bridge) can build a client without re-reading the individual parts.
# `setdefault` so an explicit `REDIS_URL` in the env still wins.
os.environ.setdefault("REDIS_URL", REDIS_URL)

# ── Celery (RabbitMQ) ─────────────────────────────────────────────────────────
_rmq_user = os.environ.get("RABBITMQ_USER",     "guest")
_rmq_pass = os.environ.get("RABBITMQ_PASSWORD", "s1Hd6lsd34lksdjldfskljsdnflssd99ef8dfsdfc2Gl1Uu")
_rmq_host = os.environ.get("RABBITMQ_HOST",     "localhost")
_rmq_port = os.environ.get("RABBITMQ_PORT",     "5672")

CELERY_BROKER_URL      = f"amqp://{_rmq_user}:{_rmq_pass}@{_rmq_host}:{_rmq_port}//"
CELERY_RESULT_BACKEND  = REDIS_URL
CELERY_ACCEPT_CONTENT  = ["json"]
CELERY_TASK_SERIALIZER = "json"
# Windows: prefork pool breaks task registry in worker child processes.
# Override with CELERY_WORKER_POOL=threads|eventlet if needed for local dev.
if sys.platform == "win32":
    CELERY_WORKER_POOL = os.environ.get("CELERY_WORKER_POOL", "solo")

# Ingestion + execution: thin masters on job_queue; LangGraph runs in
# child OS processes (see sop_ingestion/subprocess_manager.py and
# execution_app/subprocess_manager.py).
CELERY_TASK_ROUTES = {
    "sop_ingestion.run_pipeline": {"queue": "job_queue"},
    "execution_app.run_batch_async": {"queue": "job_queue"},
    "sop_ingestion.check_all_sop_revisions": {"queue": "celery"},
    "sop_ingestion.check_sop_revision": {"queue": "celery"},
}
# Max parallel ingestion subprocesses (master waits for a slot before Popen).
MAX_PIPELINE_SUBPROCESSES = int(os.environ.get("MAX_PIPELINE_SUBPROCESSES", "10"))
# Max parallel execution-batch subprocesses (same shape as the ingestion knob).
MAX_EXECUTION_SUBPROCESSES = int(os.environ.get("MAX_EXECUTION_SUBPROCESSES", "5"))

# ── Scheduled SOP revision checks (Celery Beat) ───────────────────────────────
SOP_REVISION_CHECK_ENABLED = os.environ.get(
    "SOP_REVISION_CHECK_ENABLED", "false",
).lower() in {"1", "true", "yes"}
SOP_REVISION_CHECK_HOUR = int(os.environ.get("SOP_REVISION_CHECK_HOUR", "2"))
SOP_REVISION_CHECK_MINUTE = int(os.environ.get("SOP_REVISION_CHECK_MINUTE", "0"))

CELERY_BEAT_SCHEDULE = {}
if SOP_REVISION_CHECK_ENABLED:
    CELERY_BEAT_SCHEDULE["sop-revision-check"] = {
        "task": "sop_ingestion.check_all_sop_revisions",
        "schedule": crontab(
            hour=SOP_REVISION_CHECK_HOUR,
            minute=SOP_REVISION_CHECK_MINUTE,
        ),
        "options": {"queue": "celery"},
    }

# ── Pipeline defaults (picked up by PipelineConfig.from_env()) ────────────────
SOP_MAX_DEPTH = int(os.environ.get("MAX_DEPTH", "4"))
SOP_MAX_DOCS  = int(os.environ.get("MAX_DOCS", "200"))

# ── Logging ───────────────────────────────────────────────────────────────────
# Console + rotating file for the execution engine and execution_app so
# silent-failure modes (e.g. "no shapes → default ALLOW") leave a
# breadcrumb in the worker log.
_LOG_DIR = BASE_DIR / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "engine": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class":     "logging.StreamHandler",
            "formatter": "engine",
        },
        "engine_file": {
            "class":       "logging.handlers.RotatingFileHandler",
            "filename":    str(_LOG_DIR / "execution_engine.log"),
            "maxBytes":    10 * 1024 * 1024,   # 10 MB
            "backupCount": 5,
            "formatter":   "engine",
        },
    },
    "loggers": {
        # Execution engine package + the Django app that drives it. INFO so
        # node-by-node breadcrumbs are visible; raise to WARNING in prod if
        # the volume becomes an issue.
        "uhc_execution_engine": {
            "handlers":  ["console", "engine_file"],
            "level":     "INFO",
            "propagate": False,
        },
        "execution_app": {
            "handlers":  ["console", "engine_file"],
            "level":     "INFO",
            "propagate": False,
        },
        # In-process agent_tools HTTP helper (timeouts/retries during claim tools).
        "agent_tools.http": {
            "handlers":  ["console", "engine_file"],
            "level":     "INFO",
            "propagate": False,
        },
    },
}
