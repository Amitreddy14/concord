.DEFAULT_GOAL := help
SHELL := /bin/bash

# python3 rather than python: most Linux distributions ship no `python`
# alias, and CI only appeared to work because actions/setup-python adds one.
# Override on a system where the interpreter is named differently:
#   make check PY=python
PY ?= python3
COMPOSE := docker compose -f infra/docker-compose.yml

.PHONY: require-python
require-python:
	@command -v $(PY) >/dev/null 2>&1 || { \
		echo "error: '$(PY)' not found on PATH."; \
		echo "       install Python 3.12+, or override: make <target> PY=<interpreter>"; \
		exit 1; }

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
doctor: require-python ## Verify the local inference setup and measure decode throughput
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
seed: require-python ## Load the scenario fixture dataset into the mock systems
	$(PY) -m apps.mocks.seed --fixtures evals/golden/fixtures

.PHONY: check
check: lint types contracts yaml test ## Everything CI runs

.PHONY: lint
lint: ## Lint and format check
	ruff check .
	ruff format --check .

.PHONY: types
types: ## Static type check
	mypy --strict packages apps

.PHONY: contracts
contracts: require-python ## Validate the OpenAPI contracts
	$(PY) -m scripts.validate_contracts contracts

.PHONY: yaml
yaml: require-python ## Parse every YAML document in the tree
	$(PY) -m scripts.check_yaml

.PHONY: hooks
hooks: ## Point git at the repository's tracked hooks
	git config core.hooksPath infra/githooks
	@echo "hooks enabled: $$(git config core.hooksPath)"

.PHONY: test
test: require-python ## Unit and integration tests
	pytest -q

.PHONY: eval
eval: require-python ## Run the golden scenario suite
	$(PY) -m evals.harness.run --suite evals/golden --report evals/reports

.PHONY: clean
clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .mypy_cache .ruff_cache .pytest_cache
