# Makefile for Luran

.PHONY: help install dev-install run test lint format clean docker-build docker-up docker-down migrate shell smoke-automation-flows e2e-real-tenant-enforcement seed-staging-e2e test-e2e-live

help:
	@echo "Available commands:"
	@echo "  make install       - Install production dependencies"
	@echo "  make dev-install   - Install all dependencies including dev"
	@echo "  make run           - Run development server"
	@echo "  make test          - Run tests"
	@echo "  make test-cov      - Run tests with coverage"
	@echo "  make lint          - Run linting"
	@echo "  make format        - Format code"
	@echo "  make clean         - Clean up generated files"
	@echo "  make docker-build  - Build Docker image"
	@echo "  make docker-up     - Start Docker containers"
	@echo "  make docker-down   - Stop Docker containers"
	@echo "  make migrate       - Run database migrations"
	@echo "  make shell         - Activate poetry shell"
	@echo "  make smoke-automation-flows - Run live automation/flow smoke regression"
	@echo "  make e2e-real-tenant-enforcement - Run real-account automation/flow/plan enforcement check"
	@echo "  make seed-staging-e2e - Seed deterministic staging tenants and fixtures"
	@echo "  make test-e2e-live - Run pytest-based live API E2E suite"

install:
	poetry install --only main

dev-install:
	poetry install
	poetry run pre-commit install

run:
	poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	poetry run pytest

test-cov:
	poetry run pytest --cov=app --cov-report=html --cov-report=term

lint:
	poetry run black --check app tests
	poetry run isort --check-only app tests
	poetry run ruff check app tests
	poetry run mypy app

format:
	poetry run black app tests
	poetry run isort app tests
	poetry run ruff check --fix app tests

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf .pytest_cache .coverage htmlcov/ .mypy_cache/ .ruff_cache/

docker-build:
	docker-compose build

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

migrate:
	poetry run alembic upgrade head

migrate-create:
	@read -p "Enter migration message: " msg; \
	poetry run alembic revision --autogenerate -m "$$msg"

shell:
	poetry shell

smoke-automation-flows:
	docker exec luran_api python -u /app/scripts/smoke_automation_and_flows.py

e2e-real-tenant-enforcement:
	docker exec -w /app -e PYTHONPATH=/app luran_api python -u scripts/e2e_real_tenant_enforcement.py

seed-staging-e2e:
	docker exec -w /app -e PYTHONPATH=/app luran_api python -u scripts/seed_staging_e2e.py

test-e2e-live:
	docker exec -w /app -e PYTHONPATH=/app luran_api poetry run pytest tests/e2e -m e2e -q
