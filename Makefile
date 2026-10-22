.DEFAULT_GOAL := help
SHELL := /bin/bash

# The project's own virtualenv wins when it exists, because the alternative
# produced four separate phantom failures: with the venv inactive, `python3`
# resolves to the system interpreter, which has no ruff, and `make check`
# reports a missing module as though the code were broken. `require-python`
# did not catch it — it checks that an interpreter exists, not that it is the
# right one.
#
# Still overridable, and still `python3` rather than `python` as the fallback:
# most Linux distributions ship no `python` alias, and CI only appeared to
# work because actions/setup-python adds one.
#   make check PY=python3        # ignore the venv
VENV_PY := $(CURDIR)/.venv/bin/python
PY ?= $(if $(wildcard $(VENV_PY)),$(VENV_PY),python3)

# Every Python tool is invoked as `$(PY) -m <tool>` rather than as a bare
# command, so a globally installed pytest or ruff cannot shadow the one in
# the active environment. A bare `pytest` is how `make test` passed against
# a different interpreter than the rest of the suite.
NPM ?= npm
CONSOLE := apps/console
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
migrate: require-python ## Apply database migrations
	$(PY) -m alembic -c infra/alembic.ini upgrade head

.PHONY: seed
seed: require-python ## Load the scenario fixture dataset into the mock systems
	$(PY) -m apps.mocks.seed --scenario $${SCENARIO:-S02}

.PHONY: check
check: lint types contracts yaml money deps test console ## Everything CI runs

.PHONY: lint
lint: require-python ## Lint and format check
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

.PHONY: types
types: require-python ## Static type check
	$(PY) -m mypy --strict packages apps

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

.PHONY: money
money: require-python ## Enforce the no-floats-near-money rule
	$(PY) -m scripts.check_money
	$(PY) -m scripts.check_money_ts

.PHONY: deps
deps: require-python ## Every third-party import must be a declared dependency
	$(PY) -m scripts.check_deps

.PHONY: test
test: require-python ## Unit and integration tests
	$(PY) -m pytest $(PYTEST_ARGS)

.PHONY: api
api: require-python ## Run the intake and approval API
	$(PY) -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000 --reload

.PHONY: console
console: ## Lint, type-check and test the reviewer console
	@if [ ! -d "$(CONSOLE)/node_modules" ]; then \
		echo "note: $(CONSOLE)/node_modules is absent; run 'make console-install' first."; \
		echo "      skipping the console checks rather than failing on a missing install."; \
	else \
		$(NPM) --prefix $(CONSOLE) run check; \
	fi

.PHONY: console-install
console-install: ## Install the console's dependencies
	$(NPM) --prefix $(CONSOLE) ci || $(NPM) --prefix $(CONSOLE) install

.PHONY: console-dev
console-dev: ## Run the reviewer console against a local API
	$(NPM) --prefix $(CONSOLE) run dev

.PHONY: console-build
console-build: ## Production build of the console
	$(NPM) --prefix $(CONSOLE) run build

.PHONY: eval
eval: require-python ## Run the golden scenario suite
	$(PY) -m evals.harness.run --suite evals/golden --report evals/reports

.PHONY: clean
clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .mypy_cache .ruff_cache .pytest_cache
