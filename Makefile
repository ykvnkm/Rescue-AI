PYTHONPATH := $(shell pwd)
UV := PYTHONPATH=$(PYTHONPATH) uv run
PLATFORM_COMPOSE := docker compose -f infra/docker-compose.platform.yml --env-file infra/platform.env

.PHONY: help install format lint test test-arch test-batch ci \
	up down batch-build batch-up batch-down batch-logs batch-backfill

help:
	@echo "Доступные команды:"
	@echo "  make install         - установить dev-зависимости через uv"
	@echo "  make format          - отформатировать код (black + isort)"
	@echo "  make lint            - проверить код (black/isort/flake8/mypy/pylint)"
	@echo "  make test            - запустить все тесты"
	@echo "  make test-arch       - запустить архитектурные тесты границ слоев"
	@echo "  make test-batch      - запустить batch smoke/unit тесты с порогом coverage >= 70%"
	@echo "  make ci              - локально повторить основной CI (lint + test + arch + batch)"
	@echo "  make up              - поднять основной сервис (docker compose)"
	@echo "  make down            - остановить основной сервис"
	@echo "  make batch-build     - собрать образ batch-runner для Airflow DockerOperator"
	@echo "  make batch-up        - поднять batch-платформу (Airflow-контур)"
	@echo "  make batch-down      - остановить batch-платформу"
	@echo "  make batch-logs      - смотреть логи airflow-webserver"
	@echo "  make batch-backfill  - запустить demo backfill (диапазон дат можно изменить)"

install:
	uv sync --extra dev

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
