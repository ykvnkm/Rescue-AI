PYTHONPATH := $(shell pwd)
UV := PYTHONPATH=$(PYTHONPATH) uv run
PLATFORM_COMPOSE := docker compose -f infra/docker-compose.platform.yml --env-file infra/platform.env

.PHONY: help install format lint test test-arch test-batch ci \
	up up-postgres down db-migrate batch-build batch-up batch-down batch-logs batch-backfill

help:
	@echo "Available commands:"
	@echo "  make install         - install dev dependencies with uv"
	@echo "  make format          - format code with black + isort"
	@echo "  make lint            - run black/isort/flake8/mypy/pylint"
	@echo "  make test            - run the full pytest suite"
	@echo "  make test-arch       - run architecture boundary tests"
	@echo "  make test-batch      - run batch smoke/unit tests with coverage >= 70%"
	@echo "  make ci              - run the local CI bundle"
	@echo "  make up              - start the API in memory mode"
	@echo "  make up-postgres     - start the API and Postgres from one compose file"
	@echo "  make down            - stop the local compose services"
	@echo "  make db-migrate      - apply Alembic migrations to the configured Postgres"
	@echo "  make batch-build     - build the batch-runner image for Airflow"
	@echo "  make batch-up        - start the batch platform"
	@echo "  make batch-down      - stop the batch platform"
	@echo "  make batch-logs      - tail airflow-webserver logs"
	@echo "  make batch-backfill  - run the demo Airflow backfill"

install:
	uv sync --extra dev --extra batch

format:
	$(UV) black services libs tests
	$(UV) isort services libs tests

lint:
	$(UV) black --check services libs tests
	$(UV) isort --check-only services libs tests
	$(UV) flake8 services libs tests
	$(UV) mypy services libs tests
	$(UV) pylint services libs tests

test:
	$(UV) pytest

test-arch:
	$(UV) pytest tests/architecture --no-cov

test-batch:
	$(UV) pytest tests/test_batch_runner.py tests/test_batch_stores.py tests/test_batch_smoke.py tests/test_batch_source.py tests/test_batch_main.py tests/test_batch_pilot_engine.py tests/test_batch_detector_runtime.py tests/test_batch_quality_gate.py -m "not integration" --cov=libs/batch --cov=services/batch_runner --cov-fail-under=70

ci: lint test test-arch test-batch

up:
	docker compose up --build

up-postgres:
	docker compose --profile postgres up --build

down:
	docker compose down

db-migrate:
	$(UV) alembic upgrade head

batch-build:
	$(PLATFORM_COMPOSE) --profile batch-build build batch-runner-image

batch-up:
	$(PLATFORM_COMPOSE) up -d

batch-down:
	$(PLATFORM_COMPOSE) down

batch-logs:
	$(PLATFORM_COMPOSE) logs -f airflow-webserver

batch-backfill:
	$(PLATFORM_COMPOSE) exec airflow-webserver airflow dags backfill rescue_batch_daily -s 2026-03-10 -e 2026-03-12
