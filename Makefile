# Mission Control — common entry points.
# `make` or `make help` lists everything.

.DEFAULT_GOAL := help
.PHONY: help setup up down build build-dev test test-backend test-frontend \
        migrate logs ps seed seed-clean update

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

setup: ## Generate .env with secure secrets (idempotent)
	./setup.sh

up: ## Build (if needed) and start the full stack
	docker compose up --build -d

down: ## Stop the stack
	docker compose down

build: ## Build release images (backend + frontend)
	docker compose build backend frontend

build-dev: ## Build dev-target images (hot reload, test extras)
	docker build --target dev -t mc-backend:dev ./backend
	docker build --target dev -t mc-frontend:dev ./frontend-v2

test: test-backend test-frontend ## Run all tests

test-backend: ## Backend tests (pytest — needs backend/.venv, see CONTRIBUTING)
	cd backend && .venv/bin/python -m pytest -q

test-frontend: ## Frontend tests (vitest) + type check
	cd frontend-v2 && npx tsc --noEmit && npm run test:run

migrate: ## Run DB migrations manually (they also run on backend start)
	docker compose exec backend alembic upgrade head

logs: ## Tail backend logs
	docker compose logs backend --tail=50 -f

ps: ## Show stack status
	docker compose ps

seed: ## Create the demo board (8 example tasks)
	python3 scripts/demo-seed.py

seed-clean: ## Remove the demo board
	python3 scripts/demo-seed.py --cleanup

update: ## Update an existing install (pull, refresh images, migrate)
	./install.sh --update
