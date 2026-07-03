#!/usr/bin/env bash
# Start local Docker infrastructure for development (idempotent).
#
# Usage: ./scripts/infra-up.sh
#
# Containers:
#   uhc-postgres  :5432
#   uhc-redis     :6379
#   uhc-neo4j     :7474 / :7687
#   uhc-mongo     :27017
#   uhc-rabbitmq  :5672 / :15672 (management UI)

set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "[infra] ERROR: docker not found" >&2
  exit 1
fi

run_container() {
  local name="$1"
  shift
  if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    if docker ps --format '{{.Names}}' | grep -qx "$name"; then
      echo "[infra] $name already running"
    else
      echo "[infra] starting existing container $name"
      docker start "$name" >/dev/null
    fi
  else
    echo "[infra] creating $name"
    docker run -d --name "$name" "$@" >/dev/null
  fi
}

run_container uhc-postgres \
  -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres \
  postgres:16

run_container uhc-redis \
  -p 6379:6379 \
  redis:7

run_container uhc-neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/test1234 \
  neo4j:5

run_container uhc-mongo \
  -p 27017:27017 \
  mongo:7

run_container uhc-rabbitmq \
  -p 5672:5672 -p 15672:15672 \
  rabbitmq:3.13-management

echo ""
echo "[infra] local services ready. Point .env to localhost ports, e.g.:"
echo "  PG_HOST=localhost       PG_PORT=5432       PG_PASSWORD=postgres"
echo "  REDIS_HOST=localhost    REDIS_PORT=6379"
echo "  NEO4J_HOST=localhost    NEO4J_PORT=7687     NEO4J_PASSWORD=test1234"
echo "  MONGO_HOST=localhost    MONGO_PORT=27017"
echo "  RABBITMQ_HOST=localhost RABBITMQ_PORT=5672  RABBITMQ_USER=guest RABBITMQ_PASSWORD=guest"
echo "  RabbitMQ UI: http://localhost:15672 (guest/guest)"
