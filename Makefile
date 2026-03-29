PYTHONPATH := $(shell pwd)
UV := PYTHONPATH=$(PYTHONPATH) uv run
PLATFORM_COMPOSE := docker compose -f infra/docker-compose.platform.yml --env-file infra/platform.env

.PHONY: help install format lint test test-batch ci \
	up up-postgres down batch-build batch-up batch-down batch-logs batch-backfill

help:
	@echo "Available commands:"
	@echo "  make install         - install dev dependencies via uv"
	@echo "  make format          - format code (black + isort)"
	@echo "  make lint            - check code and batch DAG syntax"
	@echo "  make test            - run all tests (unit + architecture)"
	@echo "  make ci              - full local CI (lint + test)"
	@echo "  make up              - start main service (docker compose)"
	@echo "  make down            - stop main service"

install:
	uv sync --extra dev --extra batch

format:
	$(UV) black rescue_ai tests scripts
	$(UV) isort rescue_ai tests scripts

lint:
	$(UV) black --check rescue_ai tests scripts
	$(UV) isort --check-only rescue_ai tests scripts
	$(UV) flake8 rescue_ai tests scripts
	$(UV) mypy rescue_ai tests scripts
	$(UV) pylint rescue_ai tests scripts
	python -m py_compile infra/airflow/dags/rescue_batch_daily.py

test:
	$(UV) pytest tests --cov=rescue_ai --cov-fail-under=70

ci: lint test

up:
	docker compose up --build

up-postgres:
	docker compose --profile postgres up --build

down:
	docker compose down

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
