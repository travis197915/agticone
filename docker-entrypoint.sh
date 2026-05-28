#!/usr/bin/env sh
set -eu

APP_PORT="${APP_PORT:-8000}"
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-100}"
CELERY_QUEUES="${CELERY_QUEUES:-job_queue,celery}"

echo "[entrypoint] starting Django on 0.0.0.0:${APP_PORT}"
PYTHONPATH=. python manage.py runserver "0.0.0.0:${APP_PORT}" &
DJANGO_PID=$!

echo "[entrypoint] waiting for Django to become reachable..."
for _ in $(seq 1 120); do
  if curl -fsS "http://127.0.0.1:${APP_PORT}/api/ingest/health/" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "[entrypoint] starting Celery master worker (concurrency=${CELERY_CONCURRENCY})"
PYTHONPATH=. celery -A sop_backend worker \
  -Q "${CELERY_QUEUES}" \
  --concurrency="${CELERY_CONCURRENCY}" \
  -l INFO &
CELERY_PID=$!

term_handler() {
  echo "[entrypoint] received shutdown signal"
  kill "${DJANGO_PID}" "${CELERY_PID}" 2>/dev/null || true
  wait "${DJANGO_PID}" "${CELERY_PID}" 2>/dev/null || true
}

trap term_handler INT TERM

# Exit if either process exits unexpectedly (portable /bin/sh loop).
while true; do
  if ! kill -0 "${DJANGO_PID}" 2>/dev/null; then
    echo "[entrypoint] Django exited, stopping Celery"
    kill "${CELERY_PID}" 2>/dev/null || true
    wait "${CELERY_PID}" 2>/dev/null || true
    exit 1
  fi

  if ! kill -0 "${CELERY_PID}" 2>/dev/null; then
    echo "[entrypoint] Celery exited, stopping Django"
    kill "${DJANGO_PID}" 2>/dev/null || true
    wait "${DJANGO_PID}" 2>/dev/null || true
    exit 1
  fi

  sleep 2
done
