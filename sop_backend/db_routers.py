"""Database routers.

Only one so far: send CorebackendUser reads to a separate Postgres instance
when one is configured (COREBACKEND_DB_HOST), otherwise leave it on
'default' (same instance, different schema — the normal case).
"""
from __future__ import annotations

from django.conf import settings

_COREBACKEND_MODEL_LABEL = "execution_app.CorebackendUser"


class CorebackendRouter:
    def db_for_read(self, model, **hints):
        if model._meta.label == _COREBACKEND_MODEL_LABEL and "corebackend" in settings.DATABASES:
            return "corebackend"
        return None

    def db_for_write(self, model, **hints):
        # Read-only mirror — never written through Django regardless of alias.
        return None

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if model_name == "corebackenduser":
            return False  # managed=False already blocks DDL; belt and suspenders.
        if db == "corebackend":
            return False  # nothing else may land on this alias.
        return None
