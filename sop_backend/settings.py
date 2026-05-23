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
from pathlib import Path
from dotenv import load_dotenv

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

# ── CORS (dev) ───────────────────────────────────────────────────────────────
CORS_ALLOWED_ORIGINS = [
    s.strip() for s in os.environ.get(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",") if s.strip()
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
        "NAME":     os.environ.get("PG_DATABASE", "postgres"),
        "USER":     os.environ.get("PG_USER",     "postgres"),
        "PASSWORD": os.environ.get("PG_PASSWORD", ""),
        "HOST":     os.environ.get("PG_HOST",     "localhost"),
        "PORT":     os.environ.get("PG_PORT",     "5432"),
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
_redis_user = os.environ.get("REDIS_USER",     "default")
_redis_pass = os.environ.get("REDIS_PASSWORD", "")
_redis_host = os.environ.get("REDIS_HOST",     "localhost")
_redis_port = os.environ.get("REDIS_PORT",     "6379")
_redis_auth = f"{_redis_user}:{_redis_pass}@" if _redis_pass else ""
REDIS_URL   = f"redis://{_redis_auth}{_redis_host}:{_redis_port}/0"

# ── Celery (RabbitMQ) ─────────────────────────────────────────────────────────
_rmq_user = os.environ.get("RABBITMQ_USER",     "guest")
_rmq_pass = os.environ.get("RABBITMQ_PASSWORD", "guest")
_rmq_host = os.environ.get("RABBITMQ_HOST",     "localhost")
_rmq_port = os.environ.get("RABBITMQ_PORT",     "5672")

CELERY_BROKER_URL      = f"amqp://{_rmq_user}:{_rmq_pass}@{_rmq_host}:{_rmq_port}//"
CELERY_RESULT_BACKEND  = REDIS_URL
CELERY_ACCEPT_CONTENT  = ["json"]
CELERY_TASK_SERIALIZER = "json"

# ── Pipeline defaults (picked up by PipelineConfig.from_env()) ────────────────
SOP_MAX_DEPTH    = int(os.environ.get("MAX_DEPTH",    "4"))
SOP_MAX_DOCS     = int(os.environ.get("MAX_DOCS",     "200"))
SOP_LLM_PROVIDER = os.environ.get("LLM_PROVIDER",    "anthropic")
SOP_LLM_MODEL    = os.environ.get("LLM_MODEL",        "claude-3-5-sonnet-20241022")
