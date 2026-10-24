SHELL := /bin/bash
FRONTEND_PORT ?= 3000
.PHONY: setup up down test lint format migrate seed

# The Postgres formula the Homebrew TimescaleDB build targets (e.g. postgresql@17).
pg_formula = $$(./scripts/pg-formula.sh)

setup: ## Install local services and dependencies (first run)
	./scripts/setup-local.sh
	cd backend && uv sync
	cd frontend && npm ci

up: ## Start Postgres and Redis, apply migrations, run backend and frontend (Ctrl-C stops the servers; FRONTEND_PORT=3001 to change the port)
	brew services start $(pg_formula) >/dev/null
	brew services start redis >/dev/null
	$(MAKE) migrate
	@echo "Backend http://localhost:8000  Frontend http://localhost:$(FRONTEND_PORT)"
	@trap 'kill 0' EXIT; \
	  (cd backend && uv run uvicorn app.main:app --reload --port 8000) & \
	  (cd frontend && npm run dev -- --port $(FRONTEND_PORT)) & \
	  wait

down: ## Stop Postgres and Redis
	brew services stop $(pg_formula)
	brew services stop redis

test: ## Run backend tests
	cd backend && uv run pytest

lint: ## Lint and typecheck backend and frontend
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy .
	cd frontend && npm run lint && npm run typecheck

format: ## Auto-format backend code
	cd backend && uv run ruff format . && uv run ruff check --fix .

migrate: ## Apply database migrations
	cd backend && uv run alembic upgrade head

seed: ## Load seed data
	@echo "No seed data yet; added in /p1-data-model."
