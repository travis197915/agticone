#!/bin/bash

echo "Starting all services..."

# Postgres + Redis are remapped to 5433/6380 to avoid clashing with the
# host Homebrew postgresql@14 / redis already bound to 5432/6379.
docker run -d -p 5433:5432 -e POSTGRES_PASSWORD=postgres postgres:16
echo "✅ PostgreSQL started on port 5433"

docker run -d -p 6380:6379 redis:7
echo "✅ Redis started on port 6380"

docker run -d -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/test1234 neo4j:5
echo "✅ Neo4j started on ports 7474 (HTTP) and 7687 (Bolt)"

docker run -d -p 27017:27017 mongo:7
echo "✅ MongoDB started on port 27017"

docker run -d -p 5672:5672 -p 15672:15672 rabbitmq:3.13-management
echo "✅ RabbitMQ started on ports 5672 (AMQP) and 15672 (Management UI)"

echo ""
echo "All services started!"