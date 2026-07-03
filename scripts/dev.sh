#!/usr/bin/env bash
# Run the UHC agentic backend for local development.
#
# Starts:
#   - Django (runserver)
#   - Celery worker (job_queue + celery) — required for SOP ingestion
#   - Celery Beat (optional) — scheduled revision checks when enabled in .env
#
# Usage:
#   ./scripts/dev.sh              # bootstrap venv/deps, then Django + Celery
#   ./scripts/dev.sh --beat       # also start Celery Beat
#   ./scripts/dev.sh --django-only
#   ./scripts/dev.sh --skip-install   # skip pip install (faster restart)
#   ./scripts/dev.sh --skip-migrate   # skip DB migrations
#   ./scripts/dev.sh --port 8080
#
# Bootstrap (automatic unless --skip-install):
#   - creates ../src venv if missing
#   - upgrades pip / setuptools / wheel
#   - pip install -r requirements.txt when requirements change
#   - manage.py migrate
#   - stops any stale process already bound to APP_PORT
#
# Other prerequisites:
#   - .env with PG_*, REDIS_*, RABBITMQ_*, NEO4J_*, MONGO_*, LLM keys
#   - RabbitMQ + Postgres + Redis reachable (run: make infra-up)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

APP_PORT="${APP_PORT:-8000}"
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-1}"
CELERY_QUEUES="${CELERY_QUEUES:-job_queue,celery}"
DEV_PYTHON="${DEV_PYTHON:-python3}"
RUN_DJANGO=1
RUN_CELERY=1
RUN_BEAT=0
RUN_CHECK=1
SKIP_INSTALL=0
SKIP_MIGRATE=0
VENV_DIR=""

usage() {
  sed -n '2,24p' "$0" | sed 's/^# \?//'
  echo ""
  echo "Environment overrides:"
  echo "  APP_PORT              Django port (default: 8000)"
  echo "  CELERY_CONCURRENCY    Worker concurrency (default: 1 for local dev)"
  echo "  CELERY_QUEUES         Queues to consume (default: job_queue,celery)"
  echo "  VENV                  Path to venv (default: ../src or .venv)"
  echo "  DEV_PYTHON            Python for venv creation (default: python3)"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --beat) RUN_BEAT=1; shift ;;
    --django-only) RUN_CELERY=0; RUN_BEAT=0; shift ;;
    --no-celery) RUN_CELERY=0; RUN_BEAT=0; shift ;;
    --no-check) RUN_CHECK=0; shift ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    --skip-migrate) SKIP_MIGRATE=1; shift ;;
    --port) APP_PORT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

resolve_venv_dir() {
  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    VENV_DIR="$VIRTUAL_ENV"
    return 0
  fi
  if [[ -n "${VENV:-}" ]]; then
    VENV_DIR="$VENV"
    return 0
  fi
  if [[ -f "$ROOT/../src/bin/activate" ]]; then
    VENV_DIR="$ROOT/../src"
    return 0
  fi
  if [[ -f "$ROOT/.venv/bin/activate" ]]; then
    VENV_DIR="$ROOT/.venv"
    return 0
  fi
  VENV_DIR="$ROOT/../src"
}

ensure_venv() {
  resolve_venv_dir

  if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
      echo "[dev] creating virtualenv at $VENV_DIR ..."
      "$DEV_PYTHON" -m venv "$VENV_DIR"
    fi
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"
    echo "[dev] using venv: $VENV_DIR"
  else
    echo "[dev] using active venv: $VIRTUAL_ENV"
    VENV_DIR="$VIRTUAL_ENV"
  fi
}

requirements_sha() {
  python - <<'PY'
import hashlib
from pathlib import Path
print(hashlib.sha256(Path("requirements.txt").read_bytes()).hexdigest())
PY
}

ensure_modern_pip() {
  local major minor
  read -r major minor _ <<< "$(python -m pip --version 2>/dev/null | awk '{print $2}' | tr '.' ' ')"
  major=${major:-0}
  minor=${minor:-0}
  if (( major < 23 )); then
    echo "[dev] upgrading pip/setuptools/wheel (found pip ${major}.${minor}; need >=23)..."
    python -m pip install --upgrade pip setuptools wheel
  fi
}

ensure_dependencies() {
  if [[ "$SKIP_INSTALL" -eq 1 ]]; then
    echo "[dev] skipping dependency install (--skip-install)"
    return 0
  fi

  local stamp="$VENV_DIR/.dev-requirements.sha"
  local current want
  current="$(requirements_sha)"
  want=0

  if [[ ! -f "$stamp" ]] || [[ "$(cat "$stamp")" != "$current" ]]; then
    want=1
  elif ! python - <<'PY' 2>/dev/null
import django
import uhc_sop_ingestion
import uhc_api_agent
import uhc_execution_engine
PY
  then
    want=1
  fi

  if [[ "$want" -eq 0 ]]; then
    echo "[dev] dependencies up to date"
    return 0
  fi

  echo "[dev] installing Python dependencies from requirements.txt ..."
  python -m pip install --upgrade pip setuptools wheel
  python -m pip install -r requirements.txt
  echo "$current" > "$stamp"
  echo "[dev] dependencies installed"
}

ensure_migrations() {
  if [[ "$SKIP_MIGRATE" -eq 1 ]]; then
    echo "[dev] skipping database migrations (--skip-migrate)"
    return 0
  fi
  echo "[dev] applying database migrations..."
  PYTHONPATH=. python manage.py migrate --noinput
  # If you see "models have changes not yet reflected in a migration", run
  # makemigrations locally — do not auto-generate on shared/production DBs.
}

preflight() {
  if [[ ! -f "$ROOT/.env" ]]; then
    echo "[dev] WARN: .env not found — copy from .env.example and configure credentials"
  fi
  if [[ "$RUN_CHECK" -eq 1 ]]; then
    echo "[dev] running Django system checks..."
    PYTHONPATH=. python manage.py check
  fi
}

port_in_use() {
  lsof -ti:"$APP_PORT" >/dev/null 2>&1
}

free_port() {
  local pids
  pids="$(lsof -ti:"$APP_PORT" 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    return 0
  fi
  echo "[dev] stopping stale process(es) on port $APP_PORT: $pids"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
  if port_in_use; then
    echo "[dev] WARN: port $APP_PORT still in use — try: lsof -ti:$APP_PORT | xargs kill -9" >&2
  fi
}

PIDS=()

cleanup() {
  echo ""
  echo "[dev] shutting down..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  exit 0
}

trap cleanup INT TERM

ensure_venv
ensure_modern_pip
ensure_dependencies
ensure_migrations
export PYTHONPATH=.

preflight

free_port

if port_in_use; then
  echo "[dev] ERROR: port $APP_PORT is still in use." >&2
  echo "  lsof -ti:$APP_PORT | xargs kill -9" >&2
  exit 1
fi

if [[ "$RUN_DJANGO" -eq 1 ]]; then
  echo "[dev] starting Django on http://0.0.0.0:${APP_PORT}"
  PYTHONPATH=. python manage.py runserver "0.0.0.0:${APP_PORT}" &
  DJANGO_PID=$!
  PIDS+=("$DJANGO_PID")

  echo "[dev] waiting for health endpoint..."
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${APP_PORT}/api/ingest/health/" >/dev/null 2>&1; then
      echo "[dev] Django is up"
      break
    fi
    if ! kill -0 "$DJANGO_PID" 2>/dev/null; then
      echo "[dev] ERROR: Django exited during startup" >&2
      exit 1
    fi
    sleep 1
  done
fi

if [[ "$RUN_CELERY" -eq 1 ]]; then
  echo "[dev] starting Celery worker (queues=${CELERY_QUEUES}, concurrency=${CELERY_CONCURRENCY})"
  PYTHONPATH=. celery -A sop_backend worker \
    -Q "${CELERY_QUEUES}" \
    --concurrency="${CELERY_CONCURRENCY}" \
    -l INFO &
  PIDS+=("$!")
fi

if [[ "$RUN_BEAT" -eq 1 ]]; then
  echo "[dev] starting Celery Beat (revision checks follow SOP_REVISION_CHECK_ENABLED in .env)"
  PYTHONPATH=. celery -A sop_backend beat -l INFO &
  PIDS+=("$!")
fi

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Django   http://127.0.0.1:${APP_PORT}"
echo "  Health   http://127.0.0.1:${APP_PORT}/api/ingest/health/"
echo "  API      http://127.0.0.1:${APP_PORT}/api/ingest/"
if [[ "$RUN_CELERY" -eq 1 ]]; then
  echo "  Celery   worker running (SOP ingestion requires this)"
fi
if [[ "$RUN_BEAT" -eq 1 ]]; then
  echo "  Beat     scheduler running"
fi
echo "  Press Ctrl+C to stop all services"
echo "════════════════════════════════════════════════════════════"
echo ""

while true; do
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[dev] process $pid exited — stopping remaining services" >&2
      cleanup
    fi
  done
  sleep 2
done