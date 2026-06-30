.PHONY: dev dev-beat dev-django infra-up infra-down migrate setup check

# Local development — Django + Celery worker
dev:
	@bash scripts/dev.sh

# Django + Celery worker + Beat (scheduled revision checks if enabled in .env)
dev-beat:
	@bash scripts/dev.sh --beat

# Django only (no async ingestion)
dev-django:
	@bash scripts/dev.sh --django-only

# Docker: Postgres, Redis, Neo4j, Mongo, RabbitMQ
infra-up:
	@bash scripts/infra-up.sh

infra-down:
	-docker stop uhc-postgres uhc-redis uhc-neo4j uhc-mongo uhc-rabbitmq 2>/dev/null

migrate:
	PYTHONPATH=. python manage.py migrate

setup:
	pip install -r requirements.txt
	pip install -e uhc-sop-ingestion
	pip install -e uhc-api-agent
	$(MAKE) migrate
	PYTHONPATH=. python manage.py seed_builder_catalog

check:
	PYTHONPATH=. python manage.py check
