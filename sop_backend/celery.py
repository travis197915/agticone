import os
import sys
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sop_backend.settings")

# Celery's default prefork pool is unreliable on Windows (billiard task registry
# fails → "not enough values to unpack (expected 3, got 0)" in fast_trace_task).
if sys.platform == "win32":
    os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")

app = Celery("sop_backend")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
