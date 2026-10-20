.DEFAULT_GOAL := help
SHELL := /bin/bash

PY := python
COMPOSE := docker compose -f infra/docker-compose.yml

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: bootstrap
bootstrap: ## First-time setup: models, containers, migrations, seed data
	$(MAKE) models
	$(COMPOSE) up -d
	$(MAKE) migrate
	$(MAKE) seed

.PHONY: models
models: ## Pull the local models named in .env
	ollama pull $${LLM_MODEL_PRIMARY:-qwen3:8b}
	ollama pull $${LLM_EMBEDDING_MODEL:-nomic-embed-text}

.PHONY: doctor
doctor: ## Verify the local inference setup and measure decode throughput
	$(PY) -m packages.shared.doctor

.PHONY: up
up: ## Start infrastructure and mock systems
	$(COMPOSE) up -d

.PHONY: down
down: ## Stop everything
	$(COMPOSE) down

.PHONY: logs
logs: ## Tail service logs
	$(COMPOSE) logs -f

.PHONY: migrate
migrate: ## Apply database migrations
	alembic -c infra/alembic.ini upgrade head

.PHONY: seed
seed: ## Load the scenario fixture dataset into the mock systems
	$(PY) -m apps.mocks.seed --fixtures evals/golden/fixtures

.PHONY: check
check: lint types contracts test ## Everything CI runs

.PHONY: lint
lint: ## Lint and format check
	ruff check .
	ruff format --check .

.PHONY: types
types: ## Static type check
	mypy --strict packages apps

.PHONY: contracts
contracts: ## Validate the OpenAPI contracts
	$(PY) -m scripts.validate_contracts contracts

.PHONY: test
test: ## Unit and integration tests
	pytest -q

.PHONY: eval
eval: ## Run the golden scenario suite
	$(PY) -m evals.harness.run --suite evals/golden --report evals/reports

.PHONY: clean
clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .mypy_cache .ruff_cache .pytest_cache
