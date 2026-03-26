PYTHONPATH := $(shell pwd)
UV := PYTHONPATH=$(PYTHONPATH) uv run

.PHONY: help install format lint test test-arch ci \
	up down

help:
	@echo "Available commands:"
	@echo "  make install         - install dev dependencies via uv"
	@echo "  make format          - format code (black + isort)"
	@echo "  make lint            - check code and batch DAG syntax"
	@echo "  make test            - run unit/smoke tests (no architecture)"
	@echo "  make test-arch       - run architecture boundary tests"
	@echo "  make ci              - full local CI (lint + test + arch)"
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
	$(UV) mypy rescue_ai tests
	$(UV) pylint rescue_ai tests scripts
	python -m py_compile infra/airflow/dags/rescue_batch_daily.py

test:
	$(UV) pytest tests --ignore=tests/architecture -m "not integration" --cov=rescue_ai --cov-fail-under=70

test-arch:
	$(UV) pytest tests/architecture --no-cov

ci: lint test test-arch

up:
	docker compose up --build

down:
	docker compose down
