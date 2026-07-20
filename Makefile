.DEFAULT_GOAL := help
COMPOSE := docker compose

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Install dependencies locally with uv
	uv sync --extra dev

.PHONY: build
build: ## Build docker images
	$(COMPOSE) build

.PHONY: up
up: ## Start the full stack
	$(COMPOSE) up

.PHONY: down
down: ## Stop the stack and remove containers
	$(COMPOSE) down

.PHONY: logs
logs: ## Tail logs
	$(COMPOSE) logs -f

.PHONY: shell
shell: ## Open a shell in the api container
	$(COMPOSE) exec api bash

.PHONY: migrate
migrate: ## Apply migrations
	$(COMPOSE) run --rm api alembic upgrade head

.PHONY: revision
revision: ## Autogenerate a migration: make revision m="message"
	$(COMPOSE) run --rm api alembic revision --autogenerate -m "$(m)"

.PHONY: test
test: ## Run the test suite
	$(COMPOSE) run --rm api pytest

.PHONY: lint
lint: ## Lint and type-check
	uv run ruff check src tests
	uv run mypy src

.PHONY: fmt
fmt: ## Format code
	uv run ruff format src tests
	uv run ruff check --fix src tests
