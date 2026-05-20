PYTHONPATH := $(shell pwd)
UV := PYTHONPATH=$(PYTHONPATH) uv run
HELM ?= helm
UMBRELLA_CHART := infra/k8s/charts/rescue-ai
VALUES_DIR := infra/k8s/values

.PHONY: help install format lint test ci \
	helm-check helm-deps helm-lint helm-template \
	up down \
	batch-up batch-down batch-logs batch-backfill

help:
	@echo "Available commands:"
	@echo "  make install         - install dev dependencies via uv"
	@echo "  make format          - format code (black + isort)"
	@echo "  make lint            - run static analysis (linters + DAG check)"
	@echo "  make test            - run pytest with coverage gate"
	@echo "  make helm-lint       - helm lint of all charts + template per profile"
	@echo "  make helm-template P=offline  - render a specific profile to stdout"
	@echo "  make ci              - full local CI (lint + test + helm-lint)"
	@echo "  make up              - start full local stack (docker compose)"
	@echo "  make down            - stop and remove compose stack"

install:
	uv sync --extra dev --extra batch

format:
	$(UV) black rescue_ai tests scripts infra
	$(UV) isort rescue_ai tests scripts infra

lint:
	$(UV) black --check rescue_ai tests scripts infra
	$(UV) isort --check-only rescue_ai tests scripts infra
	$(UV) flake8 rescue_ai tests scripts infra
	$(UV) mypy rescue_ai tests scripts infra
	$(UV) pylint rescue_ai tests scripts infra

test:
	$(UV) pytest tests --cov=rescue_ai --cov-fail-under=70

# ── Helm-проверки локально (полное соответствие тому, что прогоняет
#    workflow .github/workflows/k8s-lint.yml).

helm-check:
	@if ! command -v $(HELM) >/dev/null 2>&1; then \
		echo "ERROR: Helm CLI is required for helm-lint/ci but was not found."; \
		echo "Install it locally, for example: brew install helm"; \
		echo "CI installs Helm via azure/setup-helm; local machines must install it separately."; \
		exit 127; \
	fi

helm-deps: helm-check
	$(HELM) repo add bitnami https://charts.bitnami.com/bitnami --force-update
	$(HELM) repo add hashicorp https://helm.releases.hashicorp.com --force-update
	$(HELM) repo add apache-airflow https://airflow.apache.org --force-update
	$(HELM) repo update
	$(HELM) dependency update $(UMBRELLA_CHART)

helm-lint: helm-deps
	$(HELM) lint infra/k8s/charts/rescue-ai-api
	$(HELM) lint infra/k8s/charts/rescue-ai-detection
	$(HELM) lint infra/k8s/charts/rescue-ai-nav-engine
	$(HELM) lint infra/k8s/charts/rescue-ai-sync-worker
	$(HELM) lint infra/k8s/charts/rescue-ai-batch-exporter
	$(HELM) lint $(UMBRELLA_CHART)
	@for p in offline cloud dev; do \
		echo "==> helm template $$p"; \
		$(HELM) template rescue-ai $(UMBRELLA_CHART) \
			-f $(VALUES_DIR)/$$p.yaml > /tmp/rescue-ai-$$p.yaml || exit 1; \
		test -s /tmp/rescue-ai-$$p.yaml || exit 1; \
	done

# Использование: make helm-template P=vps
helm-template: helm-deps
	@if [ -z "$(P)" ]; then echo "Usage: make helm-template P=<profile>"; exit 1; fi
	$(HELM) template rescue-ai $(UMBRELLA_CHART) -f $(VALUES_DIR)/$(P).yaml

ci: lint test helm-lint

up:
	docker compose up --build -d

down:
	docker compose down

# ── batch-операции (запускаются на уже поднятом стенде, см. infra/README.md).

batch-up:
	docker compose up -d airflow-init airflow-webserver airflow-scheduler

batch-down:
	docker compose stop airflow-init airflow-webserver airflow-scheduler

batch-logs:
	docker compose logs -f airflow-webserver airflow-scheduler

batch-backfill:
	docker compose exec airflow-scheduler \
		airflow dags backfill rescue_batch_pipeline \
		--start-date 2026-03-10 --end-date 2026-03-12
