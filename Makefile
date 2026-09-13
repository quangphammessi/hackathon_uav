.DEFAULT_GOAL := help
COMPOSE := docker compose -f infra/docker-compose.yml

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up: ## Build and start the full stack (Kafka, Postgres, Redis, Ollama, 5 services, UI)
	$(COMPOSE) up --build -d
	@echo "UI      http://localhost:3000"
	@echo "Gateway http://localhost:8080/docs"

down: ## Stop the stack, keep data volumes
	$(COMPOSE) down

clean: ## Stop the stack and delete all data volumes
	$(COMPOSE) down -v

logs: ## Tail logs (make logs SERVICE=storefront)
	$(COMPOSE) logs -f $(SERVICE)

ps: ## Show stack status
	$(COMPOSE) ps

seed: ## Re-run the seed job against the running stack
	$(COMPOSE) run --rm seed

install: ## Install Python deps and the shared library for local development
	pip install -r requirements.txt
	pip install -e libs/agentmarket_core

dev: ## Run the five services locally (needs local Postgres + Redis)
	./scripts/run_local.sh start

dev-stop: ## Stop the locally-run services
	./scripts/run_local.sh stop

dev-seed: ## Apply schema and load demo data into a local Postgres
	python scripts/seed.py --reset

demo: ## Run the CLI walkthrough against a running stack
	python scripts/demo.py

test: ## Run unit tests only (no infrastructure needed)
	python -m pytest tests/unit -q

test-all: ## Run unit + integration tests (needs Postgres, Redis, and ideally a running stack)
	python -m pytest -q

ui-dev: ## Run the Next.js UI in development mode
	cd ui && npm install && npm run dev

ui-build: ## Production build of the UI
	cd ui && npm run build

.PHONY: help up down clean logs ps seed install dev dev-stop dev-seed demo test test-all ui-dev ui-build
