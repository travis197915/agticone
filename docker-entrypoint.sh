#!/usr/bin/env sh
set -eu
 
APP_PORT="${APP_PORT:-8000}"
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-3}"
CELERY_QUEUES="${CELERY_QUEUES:-job_queue,celery}"
 
# Gunicorn tuning — override via env if needed.
# --timeout       : max seconds a worker may spend on one request (default 30 = too short for /api/execute/runs/)
# --keep-alive    : must exceed Azure LB idle timeout so upstream reuse doesn't hit ECONNRESET
# --workers       : gunicorn convention — 2 * CPU cores + 1
# --graceful-timeout: allow in-flight requests to finish on SIGTERM
GUNICORN_WORKERS="${GUNICORN_WORKERS:-4}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-120}"
GUNICORN_KEEP_ALIVE="${GUNICORN_KEEP_ALIVE:-65}"
GUNICORN_GRACEFUL="${GUNICORN_GRACEFUL:-30}"
 
# ── Embedded RabbitMQ broker ──────────────────────────────────────────────
# Set EMBEDDED_RABBITMQ=0 to skip the in-container broker and point
# RABBITMQ_HOST at an external one instead.
EMBEDDED_RABBITMQ="${EMBEDDED_RABBITMQ:-1}"
RABBITMQ_USER="${RABBITMQ_USER:-guest}"
RABBITMQ_PASSWORD="${RABBITMQ_PASSWORD:-guest}"
RABBITMQ_PORT="${RABBITMQ_PORT:-5672}"
 
RABBITMQ_PID=""
 
start_rabbitmq() {
  # RabbitMQ runs as the non-root golden-image user, so point every writable
  # path at /tmp and stream logs to stdout (container-friendly).
  export HOME="${HOME:-/tmp}"
  export RABBITMQ_MNESIA_BASE="${RABBITMQ_MNESIA_BASE:-/tmp/rabbitmq/mnesia}"
  export RABBITMQ_LOGS="-"
  export RABBITMQ_NODENAME="${RABBITMQ_NODENAME:-rabbit@localhost}"
  export RABBITMQ_NODE_IP_ADDRESS="127.0.0.1"
  export RABBITMQ_NODE_PORT="${RABBITMQ_PORT}"
  mkdir -p "${RABBITMQ_MNESIA_BASE}"
 
  # RabbitMQ 4.x disables "transient non-exclusive queues" by default, but
  # Celery/kombu still declare that queue type for their mingle/pidbox control
  # and reply queues. Without re-permitting it, the worker's queue.declare is
  # rejected (INTERNAL_ERROR 541) and Celery crash-loops (RestartFreqExceeded).
  # `global_qos` is likewise deprecated in 4.x but Celery uses channel-wide
  # prefetch (basic.qos global=true), so it must be permitted too.
  # RABBITMQ_CONFIG_FILE points at the file WITHOUT the .conf suffix.
  export RABBITMQ_CONFIG_FILE="/tmp/rabbitmq/rabbitmq"
  cat > /tmp/rabbitmq/rabbitmq.conf <<'RABBITMQ_CONF'
deprecated_features.permit.transient_nonexcl_queues = true
deprecated_features.permit.global_qos = true
RABBITMQ_CONF
 
  echo "[entrypoint] starting RabbitMQ broker on 127.0.0.1:${RABBITMQ_PORT}"
  rabbitmq-server &
  RABBITMQ_PID=$!
 
  echo "[entrypoint] waiting for RabbitMQ to become ready..."
  for _ in $(seq 1 60); do
    if rabbitmqctl status >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
 
  # Provision the credentials the app expects (idempotent). The default 'guest'
  # user already exists, so we only reset its password; any other user is
  # created and granted full permissions on the default vhost '/'.
  if [ "${RABBITMQ_USER}" = "guest" ]; then
    rabbitmqctl change_password guest "${RABBITMQ_PASSWORD}" >/dev/null 2>&1 || true
  else
    rabbitmqctl add_user "${RABBITMQ_USER}" "${RABBITMQ_PASSWORD}" >/dev/null 2>&1 \
      || rabbitmqctl change_password "${RABBITMQ_USER}" "${RABBITMQ_PASSWORD}" >/dev/null 2>&1 \
      || true
    rabbitmqctl set_user_tags "${RABBITMQ_USER}" administrator >/dev/null 2>&1 || true
    rabbitmqctl set_permissions -p / "${RABBITMQ_USER}" ".*" ".*" ".*" >/dev/null 2>&1 || true
  fi
  echo "[entrypoint] RabbitMQ ready (user=${RABBITMQ_USER}, vhost=/)"
}
 
if [ "${EMBEDDED_RABBITMQ}" != "0" ]; then
  start_rabbitmq
fi
 
echo "[entrypoint] starting Django (gunicorn) on 0.0.0.0:${APP_PORT}"
echo "[entrypoint]   workers=${GUNICORN_WORKERS} timeout=${GUNICORN_TIMEOUT}s keep-alive=${GUNICORN_KEEP_ALIVE}s"
PYTHONPATH=. gunicorn sop_backend.wsgi:application \
  --bind "0.0.0.0:${APP_PORT}" \
  --workers "${GUNICORN_WORKERS}" \
  --timeout "${GUNICORN_TIMEOUT}" \
  --keep-alive "${GUNICORN_KEEP_ALIVE}" \
  --graceful-timeout "${GUNICORN_GRACEFUL}" \
  --access-logfile - \
  --error-logfile - &
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
  kill "${DJANGO_PID}" "${CELERY_PID}" ${RABBITMQ_PID:+"${RABBITMQ_PID}"} 2>/dev/null || true
  wait "${DJANGO_PID}" "${CELERY_PID}" ${RABBITMQ_PID:+"${RABBITMQ_PID}"} 2>/dev/null || true
}
 
trap term_handler INT TERM
 
# Exit if any managed process exits unexpectedly (portable /bin/sh loop).
while true; do
  if [ -n "${RABBITMQ_PID}" ] && ! kill -0 "${RABBITMQ_PID}" 2>/dev/null; then
    echo "[entrypoint] RabbitMQ exited, stopping Django + Celery"
    kill "${DJANGO_PID}" "${CELERY_PID}" 2>/dev/null || true
    wait "${DJANGO_PID}" "${CELERY_PID}" 2>/dev/null || true
    exit 1
  fi
 
  if ! kill -0 "${DJANGO_PID}" 2>/dev/null; then
    echo "[entrypoint] Django exited, stopping Celery"
    kill "${CELERY_PID}" ${RABBITMQ_PID:+"${RABBITMQ_PID}"} 2>/dev/null || true
    wait "${CELERY_PID}" ${RABBITMQ_PID:+"${RABBITMQ_PID}"} 2>/dev/null || true
    exit 1
  fi
 
  if ! kill -0 "${CELERY_PID}" 2>/dev/null; then
    echo "[entrypoint] Celery exited, stopping Django"
    kill "${DJANGO_PID}" ${RABBITMQ_PID:+"${RABBITMQ_PID}"} 2>/dev/null || true
    wait "${DJANGO_PID}" ${RABBITMQ_PID:+"${RABBITMQ_PID}"} 2>/dev/null || true
    exit 1
  fi
 
  sleep 2
done
 
 