import os
import sys
from celery import Celery

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
