PYTHONPATH := $(shell pwd)
UV := PYTHONPATH=$(PYTHONPATH) uv run

.PHONY: help install format lint test test-arch ci \
	up down

help:
	@echo "Доступные команды:"
	@echo "  make install         - установить dev-зависимости через uv"
	@echo "  make format          - отформатировать код (black + isort)"
	@echo "  make lint            - проверить код и синтаксис batch DAG"
	@echo "  make test            - запустить unit/smoke тесты без architecture"
	@echo "  make test-arch       - запустить архитектурные тесты границ слоев"
	@echo "  make ci              - локально повторить основной CI (lint + test + arch)"
	@echo "  make up              - поднять основной сервис (docker compose)"
	@echo "  make down            - остановить основной сервис"

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
	python -m py_compile infra/airflow/dags/idempotent_docker_backfill_demo.py

test:
	$(UV) pytest tests --ignore=tests/architecture -m "not integration" --cov=services --cov=libs --cov-fail-under=70

test-arch:
	$(UV) pytest tests/architecture --no-cov

ci: lint test test-arch

up:
	docker compose up --build

down:
	docker compose down
